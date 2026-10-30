import subprocess
#!/usr/bin/env python3
"""Teco.Pi web server.

Daljinski i admin za MMC: upravlja mpv-om (YouTube lista i slajdšou), izvorima
AV1/AV2/AV3/FM, FM radiom preko RTL-SDR-a, relejima i prikazuje stanje sistema.
Stranica dobija stanje preko WebSocket-a (/ws), a slike se otpremaju na /api/upload.
"""
import asyncio
import glob
import ipaddress
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
PGM_PULSE = 3   # s: impuls PGM-a (vrata, prečice PGM1/PGM2), posle se vraća na normalan rad
PGM_CMDS = {'on': 'uključen', 'off': 'isključen', 'release': 'vraćen na automatski rad', 'pulse': 'uključen na %d s' % PGM_PULSE}
NTP_CONF = Path('/etc/chrony/conf.d/teco.conf')   # chrony: izvori vremena i deljenje (Ekstenzije → NTP vreme, system/ntp.sh)
NTP_PRESETS = {'rs': ['0.rs.pool.ntp.org', '1.rs.pool.ntp.org'], 'cf': ['time.cloudflare.com']}
SMB_CONF = Path('/etc/samba/smb.conf')   # Teco.Pi ga piše ceo (deljenje fajlova iz admina)
SHARE = BASE / 'share'                   # deljeni folder na SD kartici (\\tecopi\Teco)
USB_MNT = Path('/media/teco')            # USB diskovi: /media/teco/<ime>
# slike za ekran (sat, slike, alati): /dev/shm (pola RAM-a), ne /tmp (64 MB — puni se sa 8 MB slikama na 1080p TV-u)
OVL_TMP = Path('/dev/shm') if os.path.isdir('/dev/shm') else Path('/tmp')
USB_FS = {'vfat': 'vfat', 'exfat': 'exfat', 'ntfs': 'ntfs3', 'ext4': 'ext4', 'ext3': 'ext3', 'ext2': 'ext2'}
SMB_BIN = Path('/usr/sbin/smbd')
OPL_DIRS = ('APPS', 'ART', 'CD', 'CFG', 'CHT', 'DVD', 'LNG', 'POPS', 'THM', 'VMC')   # folderi koje Open PS2 Loader očekuje
GAME_EXT = {'.iso': None, '.zso': None, '.vcd': 'POPS'}   # None: CD (do 700 MB) ili DVD
CD_MAX = 700 * 2**20
DIRECT_IP = '192.168.50.1/24'   # Pi na kablu kad je PS2 povezan direktno (OPL: PS2 192.168.50.2, server 192.168.50.1)
USER = pwd.getpwuid(os.getuid()).pw_name   # Samba deli fajlove kao ovaj korisnik (i za prijavu sa Windows-a)
STATE_FILE = DATA / 'state.json'
MPV_SOCK = '/tmp/mpvsocket'
FM_SOCK = '/tmp/mpvfm'
ST_SOCK = '/tmp/mpvstream'
HELPER_SOCKS = (FM_SOCK, ST_SOCK)   # pomoćni mpv-ovi za zvuk (FM i internet radio)
SOURCES = ('av1', 'av2', 'av3', 'av4', 'fm', 'st', 'tv', 'arc')   # arc = Arcade (emulator na Pi-ju)
ARC_DIR = DATA / 'arcade'                                            # podešavanje, save, BIOS (data/arcade/bios)
ARC_CORES = Path.home() / '.config/retroarch/cores'
ARC_EXT = ('.cue', '.chd', '.pbp', '.iso')                            # PS1 igre u folderu CD na flash-u (podfolder = igra)
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
    'devices': [],                     # Smart Home uređaji: [{'id', 'name', 'type': 'light', 'proto': 'wiz', 'ip'}]
    'idle': 'slides',                 # šta je na ekranu kad nema videa: 'slides' ili 'clock'
    'relays': [
        {'n': 'AV1', 'on': False, 'role': 'av2'},   # pinovi releja su u pins.toml (po ulozi)
        {'n': 'AV2', 'on': False, 'role': 'av3'},
        {'n': 'AV3', 'on': False, 'role': 'av4'},
        {'n': 'FM radio', 'on': False, 'role': 'tuner'},
        {'n': 'Gašenje monitora', 'on': False, 'role': 'monitor'},   # monitor je na NC kontaktu
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
# Zvukovi (Skladište → Zvukovi, SD kartica): melodije za zvono/interfon. Ugrađene se sintetišu, ostale otpremi korisnik.
EVENT_TONES = {'dingdong': 'Ding-dong', 'seven': '7-Eleven (Tajland)', 'westminster': 'Westminster', 'trio': 'Tri tona'}
TONES_DIR = EVENTS_DIR / '_tones'
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

ADMIN_CMDS = {'scr_set','scr_press', 'reboot', 'boot', 'theme','radio_clock', 'yt_cookies_del', 'wifi_scan', 'wifi_connect', 'wifi_forget', 'tv_add', 'tv_del', 'ev_set', 'ev_test', 'tone_play', 'tone_del', 'q_move',
              # Bluetooth: povezivanje već uparenih (bt_connect/bt_disconnect) je za sve, uparivanje samo Admin
              'bt_scan', 'bt_pair', 'bt_remove', 'pin_req', 'pbx_secret',
              'cast', 'alarm_cfg', 'alarm_add', 'alarm_del',   # alarmi se podešavaju u Admin → Uređaji; SMB share je u Ekstenzijama (kao WDS i DNS resolver): bez Admina
              'dev_add', 'dev_edit', 'dev_del', 'dev_scan',   # uređaji se dodaju u Adminu; dev_set (upravljanje) je za sve   # uređaji (dev_*): svi u mreži (za sada samo svetla)
              'net_mode', 'net_set', 'dns_set', 'relay', 'ir_save', 'ir_del',
              'cons', 'name', 'fm_save', 'fm_del', 'st_add', 'st_del', 'set_pin'}


def fmt_hz(f):
    return '%g kHz' % round(f / 1000, 3) if f >= 1000 else '%g Hz' % round(f, 2)


def gen_pw(n=14, special=True):
    """Nasumična lozinka: slova i brojevi bez sličnih (Il1O0o), od specijalnih znakova samo @ . ! ? (prolaze svuda)."""
    al = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'
    sp = '@.!?' if special else ''
    while True:
        pw = ''.join(secrets.choice(al + sp) for _ in range(n))
        if (not sp or any(ch in sp for ch in pw)) and any(ch.isdigit() for ch in pw) and any(ch.isupper() for ch in pw) and any(ch.islower() for ch in pw):
            return pw


# ---------------------------------------------------------------- Smart Home drajveri (po proizvođaču)

DEV_TYPES = {'light': 'Svetlo', 'appliance': 'Uređaj', 'tv': 'TV', 'camera': 'Kamera', 'door': 'Vrata', 'intercom': 'Interfon', 'bell': 'Zvono'}
RING_TYPES = ('intercom', 'bell')   # okidač je zona alarma (neko zvoni)
APPL_KINDS = {'ac': 'Klima', 'boiler': 'Bojler', 'iron': 'Pegla', 'coffee': 'Aparat za kafu', 'heater': 'Grejalica', 'washer': 'Veš mašina', 'fan': 'Ventilator', 'pump': 'Pumpa', 'other': 'Uređaj'}


class DevDriver:
    key, label, ready, dtype = '', '', False, 'light'   # ready=False: vidi se u izboru kao „uskoro“; dtype: vrsta uređaja

    async def probe(self, ip):   # dict sa podacima (npr. mac) ako uređaj odgovara, inače None
        return None

    async def poll(self, d):     # {'ok', 'on', 'dim', …}
        return {'ok': False}

    async def set(self, d, on=None, dim=None, temp=None, rgb=None, mode=None):
        return False

    async def scan(self):        # [{'ip', 'mac', 'on'}]
        return []


class WizDriver(DevDriver):
    """WiZ: lokalni UDP API (port 38899), bez oblaka. getPilot / setPilot, pretraga broadcast-om."""
    key, label, ready = 'wiz', 'WiZ', True

    @staticmethod
    def _send(ip, msg, timeout=1.5, broadcast=False):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        if broadcast:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.sendto(json.dumps(msg).encode(), (ip, 38899))
            out = []
            while True:
                try:
                    data, addr = s.recvfrom(4096)
                except socket.timeout:
                    break
                try:
                    out.append((addr[0], json.loads(data)))
                except ValueError:
                    pass
                if not broadcast:
                    break
            return out
        finally:
            s.close()

    async def call(self, ip, method, params=None, broadcast=False, timeout=1.5):
        msg = {'method': method, 'params': params or {}}
        if broadcast:
            return await asyncio.get_running_loop().run_in_executor(None, self._send, ip, msg, timeout, True)
        for _ in range(3):   # UDP preko Wi-Fi-ja: izgubljen paket nije „ne odgovara“ — pokušaj još 2 puta
            r = await asyncio.get_running_loop().run_in_executor(None, self._send, ip, msg, 0.8, False)
            if r:
                return r
        return []

    async def probe(self, ip):
        r = await self.call(ip, 'getPilot')
        return (r[0][1].get('result') or {}) if r else None

    async def poll(self, d):
        res = await self.probe(d['ip'])
        if res is None:
            return {'ok': False}
        rgb = [res.get(k) for k in ('r', 'g', 'b')]
        return {'ok': True, 'on': bool(res.get('state')), 'dim': res.get('dimming'), 'temp': res.get('temp'), 'rssi': res.get('rssi'),
                'rgb': '#%02x%02x%02x' % tuple(rgb) if all(isinstance(x, int) for x in rgb) else None}

    async def set(self, d, on=None, dim=None, temp=None, rgb=None, mode=None):
        if mode == 'max':         # najjače: tople i hladne bele diode punom snagom
            p = {'state': True, 'c': 255, 'w': 255, 'dimming': 100}
        elif temp is not None:    # bela: toplo/dnevno/hladno (WiZ 2200-6500 K)
            p = {'state': True, 'temp': max(2200, min(6500, int(temp)))}
        elif rgb:                 # boja '#rrggbb'
            h = str(rgb).lstrip('#')
            p = {'state': True, 'r': int(h[0:2], 16), 'g': int(h[2:4], 16), 'b': int(h[4:6], 16)}
        elif dim is not None:
            p = {'state': True, 'dimming': max(10, min(100, int(dim)))}
        else:
            p = {'state': bool(on)}
        return bool(await self.call(d['ip'], 'setPilot', p))

    async def scan(self):
        r = await self.call('255.255.255.255', 'getPilot', broadcast=True, timeout=2.5)
        return [{'ip': ip, 'mac': (m.get('result') or {}).get('mac'), 'on': (m.get('result') or {}).get('state')} for ip, m in r if m.get('result')]


class CameraDriver(DevDriver):
    """IP kamera (RTSP). Teco.Pi sastavlja RTSP adresu; slika ide u stranicu kao HLS (CCTV_*), samo dok neko gleda.
    probe/poll samo proveravaju da li je RTSP port otvoren (bez slike, skoro bez troška)."""
    dtype, ready = 'camera', True

    def __init__(self, key, label):
        self.key, self.label = key, label

    def url(self, d, sub=True):
        cam = d.get('cam') or {}
        if self.key == 'camurl':
            return cam.get('url') or ''
        from urllib.parse import quote
        auth = '%s:%s@' % (quote(cam.get('user') or '', safe=''), quote(cam.get('pass') or '', safe='')) if cam.get('user') else ''
        ch = int(cam.get('ch') or 1)
        if self.key == 'hikvision':   # 101 = kanal 1 glavni, 102 = kanal 1 sporedni (substream)
            path = '/Streaming/Channels/%d0%d' % (ch, 2 if sub else 1)
        else:                         # dahua: subtype 0 glavni, 1 sporedni
            path = '/cam/realmonitor?channel=%d&subtype=%d' % (ch, 1 if sub else 0)
        return 'rtsp://%s%s:%d%s' % (auth, d['ip'], int(cam.get('port') or 554), path)

    @staticmethod
    async def _port_open(host, port):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 2)
            w.close()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    def _hostport(self, d):
        from urllib.parse import urlparse
        if self.key == 'camurl':
            u = urlparse((d.get('cam') or {}).get('url') or '')
            return u.hostname, u.port or {'rtsp': 554, 'https': 443}.get(u.scheme, 80)
        return d['ip'], int((d.get('cam') or {}).get('port') or 554)

    async def probe(self, ip, d=None):
        host, port = self._hostport(d or {'ip': ip})
        return {} if host and await self._port_open(host, port) else None

    _seen = {}   # id -> (vreme provere, dostupna)

    async def poll(self, d):
        t, ok = self._seen.get(d['id'], (0, None))
        if time.time() - t < 30 and ok is not None:   # proverava na 30 s, ne na 5 s
            return {'ok': ok}
        host, port = self._hostport(d)
        ok = bool(host) and await self._port_open(host, port)
        self._seen[d['id']] = (time.time(), ok)
        return {'ok': ok}


class UsbCamDriver(DevDriver):
    """USB kamera direktno na Pi-ju (npr. Sony EyeToy, V4L2, JPEG 640x480 ~7,5 fps). Za CCTV: ffmpeg čita kameru
    i pravi H.264 hardverskim enkoderom Pi-ja (h264_v4l2m2m) -> HLS, samo dok neko gleda. Jedan strim (nema sporednog)."""
    key, label, ready, dtype = 'usbcam', 'USB kamera (na Pi-ju)', True, 'camera'

    @staticmethod
    def list_devices():
        """[{'dev': stalna putanja (by-id), 'n': naziv}] — samo prave kamere (ne enkoderi/ISP Pi-ja)."""
        out = []
        for p in sorted(glob.glob('/dev/v4l/by-id/*-video-index0')):
            name = re.sub(r'^usb-|-video-index0$', '', os.path.basename(p)).replace('_', ' ')
            out.append({'dev': p, 'n': name})
        return out

    async def probe(self, ip, d=None):
        return {} if os.path.exists(((d or {}).get('cam') or {}).get('dev') or '') else None

    async def poll(self, d):
        return {'ok': os.path.exists((d.get('cam') or {}).get('dev') or '')}

    @staticmethod
    def ffmpeg_input(d):
        return ['-f', 'v4l2', '-input_format', 'mjpeg', '-video_size', '640x480', '-framerate', '15', '-i', (d.get('cam') or {}).get('dev', '')]


class PgmDoorDriver(DevDriver):
    """Vrata/kapija/garaža preko PGM izlaza alarma (Paradox most). Nema mreže: stanje (otvoreno) iz zone alarma,
    a komanda ide kroz Teco.alarm_do (pgm pulse / on / off). Vidi Teco.dev_cmd i dev_snapshot."""
    key, label, ready, dtype = 'alarmpgm', 'Alarm PGM', True, 'door'

    async def probe(self, ip, d=None):
        return {}

    async def poll(self, d):
        return {'ok': True}


class ZoneIntercomDriver(DevDriver):
    """Interfon: okidač je zona alarma (zona se otvori kad neko zvoni). Teco pokreće obaveštenje i pamti poziv."""
    key, label, ready, dtype = 'alarmzone', 'Zona alarma', True, 'intercom'

    async def probe(self, ip, d=None):
        return {}

    async def poll(self, d):
        return {'ok': True}


class ZoneBellDriver(ZoneIntercomDriver):
    """Zvono na vratima: isto kao interfon (zona alarma se otvori kad neko pozvoni)."""
    key, label, dtype = 'bellzone', 'Zona alarma', 'bell'


class PgmApplDriver(PgmDoorDriver):
    """Uređaj (klima, bojler, pegla…) na releju PGM izlaza alarma: uključi / isključi. Stanje = PGM uključen."""
    key, label, dtype = 'applpgm', 'Alarm PGM (relej)', 'appliance'


class WizPlugDriver(WizDriver):
    """WiZ pametna utičnica: isti lokalni API kao sijalica, samo uključi / isključi."""
    key, label, dtype = 'wizplug', 'WiZ utičnica', 'appliance'

    async def set(self, d, on=None, **kw):
        return bool(await self.call(d['ip'], 'setPilot', {'state': bool(on)}))


class HttpRelayDriver(DevDriver):
    """Relej preko lokalnog HTTP API-ja, bez oblaka (Shelly, Tasmota)."""
    dtype, ready = 'appliance', True

    @staticmethod
    async def _get(url):
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
                async with s.get(url) as r:
                    return await r.json(content_type=None) if r.status == 200 else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError):
            return None


class ShellyDriver(HttpRelayDriver):
    """Shelly Gen1 (/relay/0) i Gen2+ (/rpc/Switch.*): generacija se pamti iz /shelly."""
    key, label = 'shelly', 'Shelly'
    _gen = {}

    async def probe(self, ip):
        j = await self._get('http://%s/shelly' % ip)
        if not isinstance(j, dict):
            return None
        self._gen[ip] = int(j.get('gen') or 1)
        return {'mac': j.get('mac')}

    async def _g(self, ip):
        if ip not in self._gen:
            await self.probe(ip)
        return self._gen.get(ip, 1)

    async def poll(self, d):
        if await self._g(d['ip']) >= 2:
            j = await self._get('http://%s/rpc/Switch.GetStatus?id=0' % d['ip'])
            return {'ok': True, 'on': bool(j.get('output')), 'w': j.get('apower')} if isinstance(j, dict) else {'ok': False}
        j = await self._get('http://%s/relay/0' % d['ip'])
        return {'ok': True, 'on': bool(j.get('ison'))} if isinstance(j, dict) else {'ok': False}

    async def set(self, d, on=None, **kw):
        if await self._g(d['ip']) >= 2:
            return await self._get('http://%s/rpc/Switch.Set?id=0&on=%s' % (d['ip'], 'true' if on else 'false')) is not None
        return await self._get('http://%s/relay/0?turn=%s' % (d['ip'], 'on' if on else 'off')) is not None


class TasmotaDriver(HttpRelayDriver):
    """Tasmota (Sonoff i drugi sa Tasmota firmverom): /cm?cmnd=Power."""
    key, label = 'tasmota', 'Tasmota / Sonoff'

    async def probe(self, ip):
        j = await self._get('http://%s/cm?cmnd=Status%%205' % ip)
        if not isinstance(j, dict):
            return None
        return {'mac': ((j.get('StatusNET') or {}).get('Mac'))}

    async def poll(self, d):
        j = await self._get('http://%s/cm?cmnd=Power' % d['ip'])
        if not isinstance(j, dict):
            return {'ok': False}
        v = j.get('POWER', j.get('POWER1'))
        return {'ok': True, 'on': v == 'ON'}

    async def set(self, d, on=None, **kw):
        return await self._get('http://%s/cm?cmnd=Power%%20%s' % (d['ip'], 'On' if on else 'Off')) is not None


class LgTvDriver(DevDriver):
    """LG TV (webOS): upravljanje preko mreže (SSAP, lgtv.py), paljenje Wake-on-LAN-om (TV ugašen ne sluša mrežu).
    Ključ uparivanja po TV-u: data/lgtv-<ip>.json. Pri dodavanju TV mora biti upaljen (upit za uparivanje na ekranu)."""
    key, label, ready, dtype = 'lgwebos', 'LG (webOS)', True, 'tv'
    INS = ('HDMI_1', 'HDMI_2', 'HDMI_3')
    _tv, _seen, _wake, _off = {}, {}, {}, {}

    @staticmethod
    async def _port(ip):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(ip, 3001), 1.5)
            w.close()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    @staticmethod
    def key_file(ip):
        return DATA / ('lgtv-%s.json' % ip)

    def tv(self, ip):
        import lgtv
        if ip not in self._tv:
            self._tv[ip] = lgtv.Tv(ip, str(self.key_file(ip)))
        return self._tv[ip]

    @staticmethod
    def arp_mac(ip):
        try:
            for l in Path('/proc/net/arp').read_text().splitlines()[1:]:
                f = l.split()
                if f[0] == ip and f[3] != '00:00:00:00:00:00':
                    return f[3].lower()
        except (OSError, IndexError):
            pass
        return None

    async def probe(self, ip):
        import lgtv
        try:
            await self.tv(ip).connect(pair_wait=60)   # bez ključa: TV pita „Dozvoli“ — potvrdi daljinskim
        except lgtv.TvError:
            return None
        return {'mac': self.arp_mac(ip)}

    async def poll(self, d):
        import lgtv
        if not self.key_file(d['ip']).exists():
            return {'ok': False}
        t, last = self._seen.get(d['id'], (0, None))
        waking = time.time() - self._wake.get(d['id'], 0) < 40   # posle „upali“: proverava svaki put dok se ne javi
        if time.time() < self._off.get(d['id'], 0):   # posle „ugasi“: TV se još gasi i ume da se javi kao upaljen
            return {'ok': True, 'on': False}
        if last and not last.get('on') and time.time() - t < 10 and not waking:   # ugašen: ređe
            return last
        tv, s = self.tv(d['ip']), {'ok': True}
        try:
            if not tv.up and not await self._port(d['ip']):   # ugašen TV nije na mreži: brz odgovor bez čekanja veze
                raise lgtv.TvError('nije na mreži')
            await tv.connect(pair_wait=3)   # u stanju pripreme (standby) TV primi vezu, ali se ne registruje
            p = await tv.req('ssap://com.webos.service.tvpower/power/getPowerState')
            # 'Active' = radi; 'Active Standby' / 'Suspend' / 'processing: Request Power Off' = gasi se ili je ugašen
            s['on'] = p.get('state') in ('Active', 'Screen Off') and not p.get('processing')
            if s['on']:
                a = await tv.req('ssap://audio/getStatus')
                vs = a.get('volumeStatus') or {}
                s['vol'] = a.get('volume', vs.get('volume'))
                s['mute'] = bool(a.get('mute', vs.get('muteStatus')))
                app = (await tv.req('ssap://com.webos.applicationManager/getForegroundAppInfo')).get('appId') or ''
                m = re.match(r'com\.webos\.app\.hdmi(\d)', app)
                s['in'] = 'HDMI_%s' % m.group(1) if m else ''
                s['app'] = 'YouTube' if app.startswith('youtube') else '' if m else app.split('.')[-1]
        except lgtv.TvError:
            s['on'] = False
            await tv.close()
        if waking and not s['on']:
            s['waking'] = True
        self._seen[d['id']] = (time.time(), s)
        return s

    async def set(self, d, on=None, **kw):
        import lgtv
        self._seen.pop(d['id'], None)
        if on:
            import socket
            mac = d.get('mac') or self.arp_mac(d['ip'])
            if not mac:
                return False
            self._wake[d['id']] = time.time()
            self._off.pop(d['id'], None)
            pkt = b'\xff' * 6 + bytes.fromhex(mac.replace(':', '')) * 16
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                for _ in range(3):
                    for t in ('255.255.255.255', d['ip'].rsplit('.', 1)[0] + '.255'):
                        s.sendto(pkt, (t, 9))
                    await asyncio.sleep(0.3)
            finally:
                s.close()
            return True
        try:
            await self.tv(d['ip']).req('ssap://system/turnOff')
            await self.tv(d['ip']).close()
            self._off[d['id']], self._wake[d['id']] = time.time() + 20, 0
            return True
        except lgtv.TvError:
            return False

    async def remote(self, d, k, v=None):
        """Dugmad u kartici TV-a: jačina, kanal, ulaz, YouTube. Vraća grešku (tekst) ili None."""
        import lgtv
        tv, last = self.tv(d['ip']), (self._seen.get(d['id']) or (0, {}))[1]
        self._seen.pop(d['id'], None)
        try:
            if k in ('vol+', 'vol-'):
                await tv.req('ssap://audio/volume' + ('Up' if k == 'vol+' else 'Down'))
            elif k == 'mute':
                await tv.req('ssap://audio/setMute', {'mute': not last.get('mute')})
            elif k in ('ch+', 'ch-'):   # taster daljinskog: preko SIMPLINK-a stiže i do set-top box-a
                try:
                    await tv.button('CHANNELUP' if k == 'ch+' else 'CHANNELDOWN')
                except Exception:
                    await tv.req('ssap://tv/channel' + ('Up' if k == 'ch+' else 'Down'))
            elif k == 'in' and v in self.INS:
                await tv.req('ssap://tv/switchInput', {'inputId': v})
            elif k == 'yt':
                await tv.req('ssap://system.launcher/launch', {'id': 'youtube.leanback.v4'})
            else:
                return 'Nepoznata komanda.'
        except lgtv.TvError as e:
            return str(e)
        return None


DEV_DRIVERS = {d.key: d for d in (WizDriver(), WizPlugDriver(), ShellyDriver(), TasmotaDriver(), PgmApplDriver(), LgTvDriver(),
                                   CameraDriver('hikvision', 'Hikvision'), CameraDriver('dahua', 'Dahua'), CameraDriver('camurl', 'Drugi (RTSP/HTTP adresa)'), UsbCamDriver(),
                                   PgmDoorDriver(), ZoneIntercomDriver(), ZoneBellDriver())}
CCTV_DIR = Path('/tmp/cctv')   # HLS segmenti kamera (tmpfs, RAM)
CCTV_IDLE = 20                 # s bez gledanja -> ffmpeg se gasi

# ---------------------------------------------------------------- stanje na disku

def load_state():
    st = json.loads(json.dumps(DEFAULT_STATE))
    try:
        st.update(json.loads(STATE_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return st


_state_lock = __import__('threading').Lock()


def _write_state(data):
    with _state_lock:   # upisi idu redom (poslednji pobeđuje)
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix('.tmp')
        tmp.write_text(data)
        os.replace(tmp, STATE_FILE)


def save_state(st):
    """Stanje se pretvori u tekst odmah (brzo), a upis na SD karticu ide u pozadinskoj niti:
    upis ume da traje 0,3-0,4 s i tada server (stranica, daljinski) ne bi odgovarao."""
    data = json.dumps(st, ensure_ascii=False, indent=1)
    try:
        asyncio.get_running_loop().run_in_executor(None, _write_state, data)
    except RuntimeError:   # van petlje (pokretanje): odmah
        _write_state(data)


SLIDE_W, SLIDE_H = 720, 576      # rezolucija izlaza (PAL)
SCREEN_ASPECT = 16 / 9           # oblik ekrana monitora; mpv razvlači 720x576 na ceo ekran (keepaspect=no)


def screen_vw(w, h):
    """Širina za crtanje u kvadratnim pikselima: AV (720x576) se razvlači na 16:9 ekran; HDMI (npr. 1024x600) ima kvadratne piksele."""
    return round(h * SCREEN_ASPECT) if w == 720 else w


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

    async def alive(self, timeout=4):
        """Da li jezgro mpv-a odgovara: True / False (zamrznut) / None (nije povezan).
        Posle gašenja i paljenja TV-a mpv ume da zaglavi čekajući sliku (DRM) — IPC se poveže, ali komande ne prolaze."""
        if not self.connected:
            return None
        self.rid += 1
        rid = self.rid
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:   # i slanje ima rok: zamrznut mpv ne čita socket, pa bi drain() čekao zauvek
            await asyncio.wait_for(self._send({'command': ['show-text', '', 1], 'request_id': rid}), timeout)
            await asyncio.wait_for(fut, timeout)
            return True
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            return False
        except (asyncio.CancelledError, OSError, ConnectionError):
            self.pending.pop(rid, None)
            return None


async def loop_watchdog(limit=0.3):
    """Stranica treba da odgovara odmah: ako je glavna petlja zauzeta > limit s (nešto sinhrono je blokira),
    poseban thread zapiše gde je stala (stek), najviše jednom u 30 s po istom mestu."""
    import threading
    import traceback
    import sys
    main_id = threading.get_ident()
    beat = [time.monotonic()]
    seen = {}

    def watch():
        while True:
            time.sleep(0.1)
            lag = time.monotonic() - beat[0]
            if lag > limit:
                fr = sys._current_frames().get(main_id)
                st = traceback.extract_stack(fr)[-6:] if fr else []
                where = ' <- '.join('%s:%d %s' % (os.path.basename(f.filename), f.lineno, f.name) for f in reversed(st))
                key = st[-1][:3] if st else None
                if time.monotonic() - seen.get(key, 0) > 30:
                    seen[key] = time.monotonic()
                    log.warning('petlja zauzeta %.1f s: %s', lag, where)
                while time.monotonic() - beat[0] > limit:   # isti zastoj ne prijavljuj ponovo
                    time.sleep(0.05)
    threading.Thread(target=watch, daemon=True, name='loop-watchdog').start()
    while True:
        beat[0] = time.monotonic()
        await asyncio.sleep(0.05)


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
        try:   # ID videa -> pripremljeni linkovi (važe dok ne istekne 'expire', ~6 h); čuva se i posle restarta
            self.rcache = {k: v for k, v in json.loads((DATA / 'rcache.json').read_text()).items() if v.get('exp', 0) > time.time() + 900}
        except (OSError, ValueError):
            self.rcache = {}
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
        """Releji po ulozi; pinovi su u pins.toml (izvor = 2 releja, FM = 2 releja, monitor = 1) i pale se zajedno."""
        from pins import PINS
        try:
            from gpiozero import OutputDevice
        except ImportError:
            OutputDevice = None
        for r in self.st['relays']:
            devs = []
            for pin in PINS.relay_pins(r.get('role')):
                if OutputDevice:
                    try:
                        devs.append(OutputDevice(pin, active_high=PINS.relay_active_high, initial_value=bool(r['on'])))
                    except Exception as e:  # pin zauzet ili nema GPIO-a
                        log.warning('rele %s na GPIO%s nije dostupan: %s', r['n'], pin, e)
            self.gpio.append(devs)
        for w in PINS.table()[1]:   # dupli pin, config.txt se ne slaže… (samo u zapisu servera)
            log.warning('pins.toml: %s', w)
        self._init_inputs()

    def _init_inputs(self):
        """Ulazi iz pins.toml: ima struje / na bateriji (ako su povezani) -> stanje + SMS obaveštenje."""
        from pins import PINS
        self.inputs = {}
        if not PINS.get('ulazi', 'koristi', False):
            return
        try:
            from gpiozero import DigitalInputDevice
        except ImportError:
            return
        low = bool(PINS.get('ulazi', 'aktivno_nisko', False))
        for k in ('struja', 'baterija'):
            pin = PINS.get('ulazi', k)
            if pin is None:
                continue
            try:
                dev = DigitalInputDevice(int(pin), pull_up=low, bounce_time=0.2)
            except Exception as e:
                log.warning('ulaz %s nije dostupan: %s', k, e)
                continue
            self.inputs[k] = dev
            dev.when_activated = dev.when_deactivated = lambda k=k: self._input_changed(k)

    def _input_changed(self, k):   # iz niti gpiozero
        loop = getattr(self, 'loop', None)
        if loop:
            loop.call_soon_threadsafe(self._input_event, k)

    def _input_event(self, k):
        v = self.input_state().get(k)
        log.info('ulaz %s: %s', k, v)
        if k == 'struja':
            self.notify('power', 'Struja se vratila.' if v else 'Nestalo je struje!', key='in:struja:%s' % v)
        elif k == 'baterija':
            self.notify('power', 'Teco.Pi radi na bateriji.' if v else 'Teco.Pi je ponovo na mrežnom napajanju.', key='in:bat:%s' % v)
        self.mark()

    def input_state(self):
        return {k: bool(d.value) for k, d in (getattr(self, 'inputs', None) or {}).items()}

    # ---------- dugme SOURCE na ekranu: GPIO preko tranzistora/optokaplera „pritisne“ dugme ekrana.
    # Ekran ne javlja koji je ulaz izabran, pa Teco pamti (cur) i računa koliko puta da pritisne
    # po redosledu ulaza na ekranu (order: 7" 1024x600 ekran ide HDMI → AV → VGA → HDMI…).
    SCR_DEF = {'on': False, 'order': ['HDMI', 'AV', 'VGA'], 'cur': 'HDMI', 'press_ms': 200, 'gap_ms': 700}

    def scr_cfg(self):
        from pins import PINS
        c = dict(self.SCR_DEF, **(self.st.get('scrbtn') or {}))
        c['pin'] = int(PINS.get('monitor', 'source', 6))   # pin dugmeta je u pins.toml
        return c

    def scr_dev(self, pin):
        d = getattr(self, '_scr_dev', None)
        if d and d[0] == pin:
            return d[1]
        if d and d[1]:
            d[1].close()
        dev = None
        try:
            from gpiozero import OutputDevice
            dev = OutputDevice(pin, initial_value=False)
        except Exception as e:
            log.warning('dugme ekrana na GPIO%s nije dostupno: %s', pin, e)
        self._scr_dev = (pin, dev)
        return dev

    async def scr_press(self, n=1):
        """Pritisni dugme ekrana n puta (jedan po jedan, sa razmakom da ekran stigne da promeni ulaz)."""
        c = self.scr_cfg()
        dev = self.scr_dev(int(c['pin']))
        if not dev:
            return False
        lock = self.__dict__.setdefault('_scr_lock', asyncio.Lock())
        async with lock:
            for i in range(n):
                dev.on()
                await asyncio.sleep(c['press_ms'] / 1000)
                dev.off()
                if i < n - 1:
                    await asyncio.sleep(c['gap_ms'] / 1000)
        return True

    async def scr_input(self, want):
        """Prebaci ekran na ulaz want (HDMI / AV …) brojem pritisaka od trenutnog ulaza."""
        c = self.scr_cfg()
        if not c['on'] or want not in c['order'] or c['cur'] not in c['order']:
            return
        o = c['order']
        n = (o.index(want) - o.index(c['cur'])) % len(o)
        if not n:
            return
        self.st['scrbtn'] = dict(self.st.get('scrbtn') or {}, cur=want)
        save_state(self.st)
        log.info('ekran: %s -> %s (%d pritiska)', c['cur'], want, n)
        await self.scr_press(n)
        self.mark()

    async def scr_cmd(self, c, a):
        cfg = dict(self.st.get('scrbtn') or {})
        if c == 'scr_set':   # podešavanje (Admin)
            if 'on' in a:
                cfg['on'] = bool(a['on'])
            if a.get('order'):
                order = [x.strip().upper() for x in re.split(r'[,>→\s]+', str(a['order'])) if x.strip()][:6]
                if 'HDMI' not in order or 'AV' not in order or len(set(order)) != len(order):
                    return {'err': 'Redosled mora da sadrži HDMI i AV (npr. HDMI, AV, VGA).'}
                cfg['order'] = order
            for k in ('press_ms', 'gap_ms'):
                if a.get(k) not in (None, ''):
                    try:
                        cfg[k] = max(50, min(3000, int(a[k])))
                    except (TypeError, ValueError):
                        return {'err': 'Vreme je u milisekundama.'}
            if a.get('cur'):
                cfg['cur'] = str(a['cur']).upper()
            self.st['scrbtn'] = cfg
            save_state(self.st)
            self.mark()
            if cfg.get('on'):   # odmah na ulaz koji odgovara izvoru
                await self.scr_input('AV' if self.source in AVS else 'HDMI')
            return {'ok': 'Sačuvano'}
        if c == 'scr_press':   # proba: jedan pritisak (ulaz koji Teco pamti ide jedan dalje)
            ok = await self.scr_press(1)
            if not ok:
                return {'err': 'Dugme ekrana nije dostupno.'}
            k = self.scr_cfg()
            if k['cur'] in k['order']:
                cfg['cur'] = k['order'][(k['order'].index(k['cur']) + 1) % len(k['order'])]
                self.st['scrbtn'] = cfg
                save_state(self.st)
                self.mark()
            return {'ok': 'Pritisnuto · ekran sada: ' + self.scr_cfg()['cur']}

    # ---------- Alati na ekranu (Osciloskop, Otpornici…): poseban program crta sloj PREKO plejera (mpv overlay),
    # pa YouTube/radio/TV rade ispod; gasi se samo Arcade. Podešavanje: data/tool-<ime>.json (program ga prati).
    TOOL_SCRIPTS = {'scope': 'scope.py', 'resistor': 'resistor.py', 'logic': 'logic.py', 'capacitor': 'capacitor.py', 'net': 'net.py', 'scan': 'scan.py', 'guest': 'guest.py', 'pin': 'pinout.py', 'ser': 'serial_tv.py', 'dice': 'dice.py', 'timer': 'timer.py', 'wifi': 'wifi_tv.py', 'prog': 'prog_tv.py', 'gen': 'gen_tv.py', 'bura': 'bura_tv.py', 'tran': 'tran_tv.py', 'ind': 'ind_tv.py', 'irdec': 'irdec_tv.py', 'mm': 'mm_tv.py'}
    TOOL_DEFAULTS = {
        'scope': {'run': True, 'exp': 'exp2', 'tdiv': 0.005, 'trig': {'src': 'ch1', 'level': 0.0, 'edge': 'rise'},
                  'ch1': {'on': True, 'wave': 'sine', 'freq': 50.0, 'amp': 2.0, 'offset': 0.0, 'vdiv': 1.0, 'noise': 0.03},
                  'ch2': {'on': True, 'wave': 'square', 'freq': 100.0, 'amp': 1.0, 'offset': 0.0, 'vdiv': 1.0, 'noise': 0.02}},
        'net': {}, 'scan': {}, 'guest': {'ssid': '', 'pw': '', 'sec': 'WPA', 'hidden': False},
        'capacitor': {'mode': 'code', 'code': '2A104J', 'smd': 'S5'},
        'tran': {'q': 'BC547'},   # tranzistor: oznaka ili SMD kod (components.py)
        'irdec': {},   # IR dekoder: kodove piše server (irdec_loop, RAM)
        'mm': {},      # multimetar: prikaz u RAM-u (/dev/shm/teco-mm.json)
        'ind': {'mode': 'smd', 'smd': '4R7', 'bands': ['yellow', 'violet', 'black', 'gold']},   # induktivnost: SMD/RKM kod ili prstenovi
        'pin': {'chip': {}, 'sel': -1},   # Pinout: podaci izabranog čipa dolaze sa stranice (tamo je biblioteka čipova)
        'ser': {'ts': True, 'hex': False},   # serijski terminal na ekranu: podaci idu kroz /ws/serial, ovde samo prikaz
        'dice': {'n': 2, 'sides': 6, 'vals': [], 't': 0},   # kockice: bacanje je na stranici, ekran prikazuje poslednje
        # tajmer: kraj (end) drži server, pa je isti svuda; režimi: cd odbrojavanje, sw štoperica, al alarm (HH:MM)
        'timer': {'mode': 'cd', 'dur': 300, 'left': 300, 'end': 0, 'run': False, 'label': '',
                  'sw_run': False, 'sw_start': 0, 'sw_acc': 0, 'laps': [], 'al_at': '07:00', 'al_on': False, 'al_last': '', 'ring': 0},
        'wifi': {}, 'prog': {}, 'bura': {},   # Wi-Fi analizator, programatori i vaga na ekranu: podatke piše server
        'resistor': {'mode': 'bands', 'bands': 4, 'colors': ['brown', 'black', 'red', 'gold', 'brown', 'brown'], 'smd': '103'},
        'logic': {'run': True, 'tdiv': 0.0002, 'trig': {'src': 'ch0', 'edge': 'rise'},
                  # izvor: sim (simulator) ili pk2 (PICkit 2: 3 kanala, 1024 uzorka); snimak je u data/logic-pk2.json
                  'srcmode': 'sim', 'pk2': {'rate': '1MHz', 'trig_ch': 0, 'trig': 'rise', 'win': 'start', 'uart': 0, 'baud': 9600},
                  'ch0': {'on': True, 'src': 'clock', 'freq': 10000.0, 'duty': 50.0, 'text': 'Teco', 'baud': 9600, 'bits': '10110010', 'rate': 10000.0},
                  'ch1': {'on': True, 'src': 'uart', 'freq': 1000.0, 'duty': 50.0, 'text': 'Teco.Pi', 'baud': 9600, 'bits': '1100', 'rate': 5000.0}},
    }

    def tool_cfg(self, name):
        c = json.loads(json.dumps(self.TOOL_DEFAULTS[name]))
        for k, v in ((self.st.get('tools') or {}).get(name) or {}).items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
        return c

    def comp_views(self):
        """Tranzistori i induktivnosti: podešavanje + rezultat dekodera (components.py, isti kao na TV-u)."""
        import components
        t, i = self.tool_cfg('tran'), self.tool_cfg('ind')
        key = json.dumps([t, i], sort_keys=True)
        c = getattr(self, '_comp_c', None)
        if not c or c[0] != key:   # stanje se pravi često: dekodira se samo kad se unos promeni
            c = self._comp_c = (key, {'tran': dict(t, res=components.tran(t.get('q')), ex=components.TRAN_EX,
                                                     names=sorted(k.replace('-SMD', '') for k in components.TR) + sorted(components.SMD)),
                                      'ind': dict(i, res=components.ind(i), ex=components.IND_EX['smd'], colors=components.COLORS + ['none'])})
        return c[1]

    def tool_write(self, name):
        if name == 'bura':   # vaga: stanje u RAM-u (osvežava se uživo, bura_flush)
            self.BURA_SHM.write_text(json.dumps(self.bura_view()))
            return
        if name == 'irdec':
            self.IRDEC_SHM.write_text(json.dumps(self.irdec_view()))
            return
        if name == 'mm':
            if not self.MM_SHM.exists():
                self.MM_SHM.write_text('{}')
            return
        f = DATA / ('tool-%s.json' % name)
        tmp = f.with_suffix('.tmp')
        data = (self.nt_view() if name == 'net' else self.scan_view() if name == 'scan' else getattr(self, 'wifi_last', None) or {} if name == 'wifi'
                else self.prog_view() if name == 'prog' else self.gen_view() if name == 'gen' else self.tool_cfg(name))
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, f)   # program na ekranu vidi novu verziju (mtime) i odmah je primeni

    def tool_running(self):
        t = getattr(self, 'tool', None)
        return t['name'] if t and t['proc'].returncode is None else None

    TOOL_BLACK = OVL_TMP / 'teco-black.png'

    def is_black(self):
        """U plejeru je crna podloga za alat (nije video: ne prikazuje se kao YouTube)."""
        return (self.mpv.props.get('path') or '') == str(self.TOOL_BLACK)

    async def tool_base(self):
        """mpv koji ništa ne pušta (prazan) ne crta slojeve — alat se ne bi video. Tada se učita crna slika kao podloga."""
        if not self.mpv.connected or not self.mpv.props.get('idle-active'):
            return
        if not self.TOOL_BLACK.exists():
            from PIL import Image
            Image.new('RGB', (16, 16)).save(self.TOOL_BLACK)
        await self.mpv.cmd('set_property', 'image-display-duration', 'inf')
        await self.mpv.cmd('loadfile', str(self.TOOL_BLACK), 'replace')
        await self.mpv.cmd('set_property', 'pause', False)
        self.tool_black = True

    async def tool_base_off(self):
        if getattr(self, 'tool_black', False) or self.is_black():   # i posle restarta servera (zastavica izgubljena)
            self.tool_black = False
            if self.is_black():
                await self.mpv.cmd('stop')
                await self.mpv.cmd('set_property', 'image-display-duration', self.st['slide_sec'])

    async def tool_show(self, name):
        if name not in self.TOOL_SCRIPTS:
            return {'err': 'Nepoznat alat.'}
        if self.arc_running():
            await self.arc_stop()
        if self.tool_running():
            await self.tool_hide(restore=False)
        self.tool_write(name)
        if self.source in AVS:   # ekran je na AV ulazu (konzola): alat je na HDMI-ju
            asyncio.get_running_loop().create_task(self.scr_input('HDMI'))
        await self.tool_base()   # ulaz na TV-u (Uređaji) se ne menja sam — samo ručno
        proc = await asyncio.create_subprocess_exec('python3', str(BASE / 'tools' / self.TOOL_SCRIPTS[name]), str(DATA / ('tool-%s.json' % name)),
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=open('/tmp/teco-tool.log', 'w'))
        self.tool = {'name': name, 'proc': proc}
        log.info('alat na ekranu: %s', name)

        async def watch():
            await proc.wait()
            if getattr(self, 'tool', None) and self.tool['proc'] is proc and not self.tool.get('closing'):
                log.warning('alat %s se ugasio sam (vidi /tmp/teco-tool.log)', name)
                await self.tool_hide()
        asyncio.get_running_loop().create_task(watch())
        if name == 'net' and not self.nt_view()['t']:   # prvi ulazak: test odmah
            await self.net_test()
        if name == 'scan' and not self.scan_db()['last']:
            await self.scan_run()
        self.mark()
        return {'ok': 'Na ekranu: ' + {'scope': 'Osciloskop', 'resistor': 'Otpornici', 'logic': 'Logički analizator', 'capacitor': 'Kondenzatori', 'net': 'Mrežni tester', 'scan': 'Skeniranje mreže', 'guest': 'Wi-Fi za goste',
                                      'pin': 'Pinout', 'ser': 'Serijski terminal', 'dice': 'Kockice', 'timer': 'Tajmer', 'prog': 'Programator',
                                      'wifi': 'Wi-Fi analizator', 'gen': 'Generator signala', 'bura': 'BURAscale', 'tran': 'Tranzistori',
                                      'ind': 'Induktivnosti', 'irdec': 'IR dekoder', 'mm': 'Multimetar'}.get(name, name)}

    async def tool_hide(self, restore=True):
        t = getattr(self, 'tool', None)
        if not t:
            return {}
        t['closing'] = True
        if t['proc'].returncode is None:
            t['proc'].terminate()
            try:
                await asyncio.wait_for(t['proc'].wait(), 5)
            except asyncio.TimeoutError:
                t['proc'].kill()
        self.tool = None
        await self.mpv.cmd('overlay-remove', 63)   # sloj alata (ako ga program nije stigao da skloni)
        if restore:   # bez alata: crna podloga više ne treba (slike / sat se vraćaju kao pre)
            await self.tool_base_off()
        if restore and self.source in AVS:   # ekran nazad na AV ulaz konzole
            await self.scr_input('AV')
        log.info('alat sklonjen sa ekrana')
        self.mark()
        return {'ok': 'Alat sklonjen sa ekrana'}

    # ---------- Mrežni tester (Alati): sve provere redom (veza, Wi-Fi/kabl, port na sviču, ruter, DNS, internet, brzina).
    # Rezultat ide na stranicu (snapshot 'nettest') i na ekran (tools/net.py čita data/tool-net.json).
    NT_STEPS = [('link', 'Aktivna veza'), ('wifi', 'Wi-Fi'), ('eth', 'Kabl (eth0)'), ('port', 'Port na ruteru / sviču'),
                ('dhcp', 'Adresa (DHCP)'), ('gw', 'Ruter (gateway)'), ('lan', 'Lokalna mreža'), ('dns', 'DNS'),
                ('ping', 'Internet (ping)'), ('mtu', 'MTU'), ('http', 'HTTPS'), ('pub', 'Javna adresa'),
                ('trace', 'Putanja do interneta'), ('vpn', 'VPN (tun0)'), ('down', 'Brzina preuzimanja'), ('up', 'Brzina slanja')]

    def nt_view(self):
        nt = getattr(self, 'nettest', None)
        if not nt:
            nt = {'running': False, 't': 0, 'step': None, 'items': [{'k': k, 'n': n, 's': 'wait', 'v': '', 'd': ''} for k, n in self.NT_STEPS]}
            self.nettest = nt
        return nt

    def nt_set(self, k, s, v='', d=''):
        for it in self.nt_view()['items']:
            if it['k'] == k:
                it.update(s=s, v=str(v)[:80], d=str(d)[:160])
        if self.tool_running() == 'net':
            self.tool_write('net')
        self.mark()

    @staticmethod
    async def _ping(host, n=10, extra=()):
        out = await Teco._run('ping', '-n', '-q', '-c', str(n), '-i', '0.2', '-W', '1', *extra, host, timeout=n * 1.5 + 5)
        m = re.search(r'(\d+) packets transmitted, (\d+) received', out)
        rtt = re.search(r'= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms', out)
        sent, recv = (int(m.group(1)), int(m.group(2))) if m else (n, 0)
        return {'loss': round(100 * (sent - recv) / max(1, sent)), 'avg': float(rtt.group(2)) if rtt else None,
                'max': float(rtt.group(3)) if rtt else None, 'jit': float(rtt.group(4)) if rtt else None}

    async def net_test(self):
        if self.nt_view()['running']:
            return {'err': 'Test je već u toku.'}
        self.nettest = None
        nt = self.nt_view()
        nt.update(running=True, t=time.time())
        asyncio.get_running_loop().create_task(self._net_test())
        self.mark()
        return {'ok': 'Mrežni test pokrenut'}

    async def _net_test(self):
        import aiohttp
        import socket
        nt = self.nettest
        ok_net = True
        try:
            await self.net_refresh()
            net = self.net
            gw = net.get('gw')
            # --- aktivna veza (preko čega ide internet)
            nt['step'] = 'link'
            r = json.loads(await self._run('ip', '-j', 'route', 'get', '1.1.1.1', timeout=5) or '[]')
            dev = r[0].get('dev') if r else None
            src = r[0].get('prefsrc') if r else None
            ifs = {i['n']: i for i in net.get('ifs', [])}
            if dev:
                self.nt_set('link', 'ok', '%s · %s' % ({'wlan0': 'Wi-Fi', 'eth0': 'kabl'}.get(dev, dev), src or ''),
                            'MAC %s · ime %s' % (ifs.get(dev, {}).get('mac', '?'), net.get('host')))
            else:
                self.nt_set('link', 'fail', 'nema rute ka internetu', 'Ni Wi-Fi ni kabl nemaju gateway')
                ok_net = False
            # --- Wi-Fi: pristupna tačka (BSSID), kanal, signal, brzina veze, susedne mreže na istom kanalu
            nt['step'] = 'wifi'
            out = await self._run('nmcli', '-t', '-f', 'IN-USE,SSID,BSSID,CHAN,FREQ,RATE,SIGNAL,SECURITY', 'dev', 'wifi', 'list', '--rescan', 'no', timeout=8)
            rows = [re.split(r'(?<!\\):', l) for l in out.splitlines()]
            cur = next((x for x in rows if x and x[0] == '*' and len(x) >= 8), None)
            if cur:
                ch = cur[3]
                same = sum(1 for x in rows if len(x) >= 8 and x[3] == ch and x[0] != '*')
                dbm = None
                try:
                    m = re.search(r'wlan0:\s+\d+\s+[\d.]+\s+(-?\d+)', Path('/proc/net/wireless').read_text())
                    dbm = int(m.group(1)) if m else None
                except OSError:
                    pass
                sig = int(cur[6] or 0)
                self.nt_set('wifi', 'ok' if sig >= 60 else 'warn' if sig >= 35 else 'fail',
                            '%s · signal %d%%%s' % (cur[1].replace('\\:', ':'), sig, (' (%d dBm)' % dbm) if dbm else ''),
                            'pristupna tačka %s · kanal %s (%s) · %s · %s · %d mreža na istom kanalu'
                            % (cur[2].replace('\\:', ':'), ch, cur[4], cur[5], cur[7] or 'otvorena', same))
            else:
                self.nt_set('wifi', 'info', 'nije povezan', 'Wi-Fi radio: ' + ('uključen' if net.get('radio') else 'isključen'))
            # --- kabl: link, brzina, duplex
            nt['step'] = 'eth'
            sysd = Path('/sys/class/net/eth0')

            def rd(f):
                try:
                    return (sysd / f).read_text().strip()
                except OSError:
                    return ''
            carrier = rd('carrier') == '1'
            direct = False
            if carrier:
                spd, dup = rd('speed'), rd('duplex')
                ip4 = ', '.join(ifs.get('eth0', {}).get('ip') or []) or 'bez adrese'
                direct = '192.168.50.1/24' in ip4
                self.nt_set('eth', 'ok' if spd == '100' and dup == 'full' else 'warn', '%s Mbit/s · %s duplex' % (spd or '?', dup or '?'),
                            ('PS2 direktno · ' if direct else '') + ip4 + ' · greške RX/TX %s/%s' % (rd('statistics/rx_errors'), rd('statistics/tx_errors')))
            else:
                self.nt_set('eth', 'info', 'kabl nije uključen', 'nema signala na portu (carrier 0)')
            # --- port na sviču / ruteru: LLDP / CDP (do 35 s, samo ako je kabl uključen)
            nt['step'] = 'port'
            if carrier and direct:
                self.nt_set('port', 'info', 'kabl je na PS2 (direktno)', ('Internet ide preko Wi-Fi-ja: pristupna tačka %s, kanal %s' % (cur[2].replace('\\:', ':'), cur[3])) if cur else '')
            elif carrier:
                self.nt_set('port', 'run', 'slušam LLDP / CDP do 35 s…')
                out = await self._run('sudo', '-n', 'python3', str(BASE / 'tools' / 'lldp.py'), 'eth0', '35', timeout=45)
                try:
                    p = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
                except ValueError:
                    p = {}
                if p.get('port'):
                    self.nt_set('port', 'ok', 'port %s%s' % (p['port'], (' (' + p['pdesc'] + ')') if p.get('pdesc') and p['pdesc'] != p['port'] else ''),
                                '%s: %s%s%s' % (p['proto'], p.get('sys') or p.get('chassis') or p.get('src'),
                                                (' · VLAN %s' % p['vlan']) if p.get('vlan') else '', (' · ' + p['mgmt']) if p.get('mgmt') else ''))
                else:
                    self.nt_set('port', 'info', 'ruter ne javlja port', 'Nema LLDP/CDP okvira za 35 s — obični kućni ruteri ih ne šalju (upravljivi svičevi da)')
            elif cur:
                self.nt_set('port', 'info', 'Wi-Fi: kanal %s' % cur[3], 'Preko Wi-Fi-ja nema porta; Pi je na pristupnoj tački ' + cur[2].replace('\\:', ':'))
            else:
                self.nt_set('port', 'info', 'nema kabla', '')
            # --- DHCP zakup
            nt['step'] = 'dhcp'
            if dev:
                out = await self._run('nmcli', '-t', '-f', 'DHCP4', 'dev', 'show', dev, timeout=5)
                o = dict(re.findall(r'option\[\d+\]:(\w+) = (.*)', out) or re.findall(r'DHCP4\.OPTION\[\d+\]:(\w+) = (.*)', out))
                if o:
                    lt = int(o.get('dhcp_lease_time', 0) or 0)
                    self.nt_set('dhcp', 'ok', '%s / %s' % (o.get('ip_address', src), o.get('subnet_mask', '?')),
                                'DHCP server %s · zakup %s' % (o.get('dhcp_server_identifier', '?'), ('%d h' % (lt // 3600)) if lt >= 3600 else ('%d min' % (lt // 60))))
                else:
                    self.nt_set('dhcp', 'info', 'statička adresa', src or '')
            else:
                self.nt_set('dhcp', 'fail', 'nema adrese')
            # --- ruter: ping, MAC
            nt['step'] = 'gw'
            if gw:
                self.nt_set('gw', 'run', gw + ' · ping…')
                pg = await self._ping(gw, 20)
                nb = await self._run('ip', 'neigh', 'show', gw, timeout=5)
                gmac = (re.search(r'lladdr (\S+)', nb) or [None, '?'])[1]
                st = 'fail' if pg['avg'] is None else 'warn' if pg['loss'] > 0 or pg['avg'] > 30 else 'ok'
                self.nt_set('gw', st, '%s · %s' % (gw, ('%.1f ms' % pg['avg']) if pg['avg'] is not None else 'ne odgovara'),
                            'gubitak %d%% · jitter %s ms · max %s ms · MAC %s' % (pg['loss'], pg['jit'], pg['max'], gmac))
                if pg['avg'] is None:
                    ok_net = False
            else:
                self.nt_set('gw', 'fail', 'nema gateway-a')
                ok_net = False
            # --- lokalna mreža: uređaji koje Pi vidi (ARP)
            nt['step'] = 'lan'
            nb = await self._run('ip', 'neigh', timeout=5)
            alive = [l for l in nb.splitlines() if 'lladdr' in l and ('REACHABLE' in l or 'STALE' in l or 'DELAY' in l)]
            self.nt_set('lan', 'info', '%d uređaja viđeno' % len(alive), '%d trenutno odgovara' % sum('REACHABLE' in l for l in alive))
            # --- DNS
            nt['step'] = 'dns'
            servers = (net.get('dns') or {}).get('active') or []
            loop = asyncio.get_running_loop()
            t0 = time.time()
            try:
                ai = await asyncio.wait_for(loop.getaddrinfo('www.google.com', 443, type=socket.SOCK_STREAM), 6)
                ms = (time.time() - t0) * 1000
                self.nt_set('dns', 'ok' if ms < 150 else 'warn', '%.0f ms · %s' % (ms, ai[0][4][0]), 'server: ' + (', '.join(servers) or '?'))
            except (OSError, asyncio.TimeoutError):
                self.nt_set('dns', 'fail', 'ne razrešava imena', 'server: ' + (', '.join(servers) or '?'))
            # --- internet ping
            nt['step'] = 'ping'
            self.nt_set('ping', 'run', '1.1.1.1 i 8.8.8.8…')
            a, b = await asyncio.gather(self._ping('1.1.1.1', 15), self._ping('8.8.8.8', 15))
            best = min((x for x in (a, b) if x['avg'] is not None), key=lambda x: x['avg'], default=None)
            if best:
                st = 'warn' if max(a['loss'], b['loss']) > 2 or best['avg'] > 80 else 'ok'
                self.nt_set('ping', st, '%.0f ms' % best['avg'], '1.1.1.1: %s ms, gubitak %d%% · 8.8.8.8: %s ms, gubitak %d%%'
                            % (a['avg'], a['loss'], b['avg'], b['loss']))
            else:
                self.nt_set('ping', 'fail', 'nema odgovora', 'Internet nije dostupan (ili je ping blokiran)')
            # --- MTU (1500 bez fragmentacije)
            nt['step'] = 'mtu'
            m1 = await self._run('ping', '-n', '-c', '1', '-W', '2', '-M', 'do', '-s', '1472', '8.8.8.8', timeout=6)
            if ' 0% packet loss' in m1:
                self.nt_set('mtu', 'ok', '1500 prolazi', 'paketi pune veličine bez fragmentacije')
            else:
                for size in (1464, 1452, 1432, 1400, 1300):
                    if ' 0% packet loss' in await self._run('ping', '-n', '-c', '1', '-W', '2', '-M', 'do', '-s', str(size), '8.8.8.8', timeout=6):
                        self.nt_set('mtu', 'info' if size + 28 == 1492 else 'warn', 'MTU ~%d' % (size + 28),
                                    'PPPoE veza provajdera (uobičajeno)' if size + 28 == 1492 else 'manji MTU (VPN / tunel?) — veliki paketi se dele')
                        break
                else:
                    self.nt_set('mtu', 'info', 'nije izmereno', '')
            # --- HTTPS + javna adresa
            to = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=to) as ses:
                nt['step'] = 'http'
                try:
                    t0 = time.time()
                    async with ses.get('https://www.google.com/generate_204') as rq:
                        ms = (time.time() - t0) * 1000
                        self.nt_set('http', 'ok' if rq.status == 204 else 'warn', '%.0f ms' % ms,
                                    'HTTP %d%s' % (rq.status, '' if rq.status == 204 else ' — možda portal za prijavu (hotel/javni Wi-Fi)'))
                except Exception as e:
                    self.nt_set('http', 'fail', 'ne radi', type(e).__name__)
                nt['step'] = 'pub'
                try:
                    async with ses.get('https://ipinfo.io/json') as rq:
                        j = await rq.json(content_type=None)
                    self.nt_set('pub', 'info', j.get('ip', '?'), ' · '.join(x for x in (j.get('org'), j.get('city'), j.get('country')) if x))
                except Exception as e:
                    self.nt_set('pub', 'warn', 'nepoznata', type(e).__name__)
                # --- putanja (traceroute)
                nt['step'] = 'trace'
                self.nt_set('trace', 'run', 'traceroute…')
                out = await self._run('traceroute', '-n', '-q', '1', '-w', '1', '-m', '15', '8.8.8.8', timeout=40)
                hops = re.findall(r'^\s*(\d+)\s+(\S+)(?:\s+([\d.]+) ms)?', out, re.M)
                if hops:
                    shown = ' → '.join(h[1] for h in hops[:6]) + (' → …' if len(hops) > 6 else '')
                    self.nt_set('trace', 'info', '%d skokova' % len(hops), shown)
                else:
                    self.nt_set('trace', 'info', 'nije izmereno', '')
                # --- VPN
                nt['step'] = 'vpn'
                if 'tun0' in ifs:
                    pg = await self._ping('10.10.255.1', 5)
                    self.nt_set('vpn', 'ok' if pg['avg'] is not None else 'warn', ', '.join(ifs['tun0'].get('ip') or []) or 'tun0',
                                ('server 10.10.255.1: %.0f ms' % pg['avg']) if pg['avg'] is not None else 'VPN server ne odgovara')
                else:
                    self.nt_set('vpn', 'info', 'nije aktivan', '')
                # --- brzina: preuzimanje (do 25 MB / 10 s), slanje (5 MB)
                nt['step'] = 'down'
                if ok_net:
                    self.nt_set('down', 'run', 'merim…')
                    try:
                        got, t0 = 0, time.time()
                        async with ses.get('https://speed.cloudflare.com/__down?bytes=25000000', timeout=aiohttp.ClientTimeout(total=20)) as rq:
                            async for chunk in rq.content.iter_chunked(65536):
                                got += len(chunk)
                                el = time.time() - t0
                                if el > 0.5:
                                    next(it for it in nt['items'] if it['k'] == 'down')['v'] = '%.1f Mbit/s…' % (got * 8 / el / 1e6)
                                    self.mark()
                                if el > 10:
                                    break
                        mb = got * 8 / (time.time() - t0) / 1e6
                        self.nt_set('down', 'ok' if mb >= 10 else 'warn', '%.1f Mbit/s' % mb, '%.1f MB za %.1f s (Cloudflare)' % (got / 1e6, time.time() - t0))
                    except Exception as e:
                        self.nt_set('down', 'fail', 'nije uspelo', type(e).__name__)
                    nt['step'] = 'up'
                    self.nt_set('up', 'run', 'merim…')
                    try:
                        data = os.urandom(5_000_000)
                        t0 = time.time()
                        async with ses.post('https://speed.cloudflare.com/__up', data=data, timeout=aiohttp.ClientTimeout(total=30)) as rq:
                            await rq.read()
                        el = time.time() - t0
                        mb = len(data) * 8 / el / 1e6
                        self.nt_set('up', 'ok' if mb >= 5 else 'warn', '%.1f Mbit/s' % mb, '5 MB za %.1f s' % el)
                    except Exception as e:
                        self.nt_set('up', 'fail', 'nije uspelo', type(e).__name__)
                else:
                    self.nt_set('down', 'info', 'preskočeno (nema interneta)')
                    self.nt_set('up', 'info', 'preskočeno (nema interneta)')
        except Exception:
            log.exception('mrežni test')
        finally:
            for it in nt['items']:
                if it['s'] in ('wait', 'run'):
                    it['s'], it['v'] = 'info', it['v'] or 'nije provereno'
            nt.update(running=False, step=None, done=time.time())
            if self.tool_running() == 'net':
                self.tool_write('net')
            self.mark()

    # ---------- Skeniranje mreže (Alati): uređaji na mrežama Pi-ja + poznati portovi; spisak se čuva (data/netscan.json)
    SCAN_FILE = DATA / 'netscan.json'

    def scan_db(self):
        if getattr(self, '_scan_db', None) is None:
            try:
                self._scan_db = json.loads(self.SCAN_FILE.read_text())
            except (OSError, ValueError):
                self._scan_db = {'devices': {}, 'last': 0, 'ifs': ['wlan0', 'eth0']}
        return self._scan_db

    def scan_save(self):
        tmp = self.SCAN_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.scan_db(), ensure_ascii=False))
        os.replace(tmp, self.SCAN_FILE)

    def scan_view(self):
        db, st = self.scan_db(), getattr(self, 'scan_st', None) or {}
        devs = sorted(({'key': k, **d} for k, d in db['devices'].items()),
                      key=lambda d: (not d.get('online'), tuple(int(x) for x in d['ip'].split('.')) if d.get('ip') else (999,)))
        ifs = [{'n': i['n'], 'ip': i['ip']} for i in self.net.get('ifs', []) if i.get('ip') and i['n'] != 'lo']
        return {'running': bool(st.get('running')), 'step': st.get('step', ''), 'prog': st.get('prog', 0), 'last': db['last'],
                'sel': db.get('ifs', []), 'ifs': ifs, 'devices': devs}

    def scan_touch(self, **kw):
        self.scan_st.update(kw)
        if self.tool_running() == 'scan':
            self.tool_write('scan')
        self.mark()

    async def wol_send(self, mac):
        """Wake-on-LAN paket: 6×FF + 16× MAC, UDP broadcast (port 9 i 7) na svim mrežama Pi-ja."""
        import socket
        pkt = b'\xff' * 6 + bytes.fromhex(mac.replace(':', '')) * 16
        targets = {'255.255.255.255'}
        for i in self.net.get('ifs', []):
            if i['n'] in ('lo', 'tun0'):
                continue
            for a in i.get('ip') or []:
                targets.add(str(ipaddress.ip_interface(a).network.broadcast_address))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            for _ in range(3):
                for t in targets:
                    for port in (9, 7):
                        try:
                            s.sendto(pkt, (t, port))
                        except OSError:
                            pass
                await asyncio.sleep(0.3)
        finally:
            s.close()
        return targets

    async def wol(self, key):
        """Wake-on-LAN za uređaj iz spiska skeniranja; zatim 2 min čeka da se uređaj javi."""
        db = self.scan_db()
        d = db['devices'].get(key)
        if not d or not d.get('mac'):
            return {'err': 'Uređaj nema MAC adresu.'}
        if bytes.fromhex(d['mac'].replace(':', ''))[0] & 2:
            return {'err': 'Privatna (nasumična) MAC adresa — buđenje ne radi.'}
        targets = await self.wol_send(d['mac'])
        d['wake'] = {'t': time.time(), 'st': 'wait'}
        self.mark()
        log.info('Wake-on-LAN: %s (%s)', d.get('label') or d.get('name') or d['mac'], ', '.join(sorted(targets)))

        async def watch():   # da li se upalio (ping na poslednju poznatu IP, ili ARP po MAC-u)
            import netscan
            sem = asyncio.Semaphore(1)
            for _ in range(24):
                await asyncio.sleep(5)
                ip = d.get('ip')
                if not ip:
                    for l in (await self._run('ip', 'neigh', timeout=5)).splitlines():
                        if d['mac'] in l.lower() and 'FAILED' not in l:
                            ip = d['ip'] = l.split()[0]
                if ip and await netscan.ping(ip, sem):
                    d['wake'] = {'t': time.time(), 'st': 'up'}
                    d['online'], d['last'] = True, time.time()
                    break
            else:
                d['wake'] = {'t': time.time(), 'st': 'none'}
            self.scan_save()
            self.mark()
        asyncio.get_running_loop().create_task(watch())
        return {'ok': 'Paket za buđenje poslat · čekam da se uređaj javi (do 2 min)'}

    async def scan_run(self, sel=None):
        if (getattr(self, 'scan_st', None) or {}).get('running'):
            return {'err': 'Skeniranje je već u toku.'}
        db = self.scan_db()
        if isinstance(sel, list):
            db['ifs'] = [str(x)[:15] for x in sel][:6]
        self.scan_st = {'running': True, 'step': 'pokrećem…', 'prog': 0}
        asyncio.get_running_loop().create_task(self._scan())
        self.mark()
        return {'ok': 'Skeniranje mreže pokrenuto'}

    async def _scan(self):
        import socket
        import netscan
        db = self.scan_db()
        try:
            await self.net_refresh()
            gw = self.net.get('gw')
            mf = DATA / 'manuf'   # baza proizvođača (Wireshark): preuzima se ako je nema ili je starija od 90 dana
            if not mf.exists() or time.time() - mf.stat().st_mtime > 90 * 86400:
                self.scan_touch(step='preuzimam bazu proizvođača…', prog=2)
                try:
                    import aiohttp
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as ses:
                        async with ses.get(netscan.MANUF_URL) as rq:
                            body = await rq.read() if rq.status == 200 else b''
                    if body.count(b'\n') > 10000:
                        mf.with_suffix('.tmp').write_bytes(body)
                        os.replace(mf.with_suffix('.tmp'), mf)
                except Exception as e:
                    log.warning('baza proizvođača: %s', e)
            if netscan._manuf is None:
                await asyncio.get_running_loop().run_in_executor(None, netscan.load_manuf, str(mf))
            nets = netscan.networks([i for i in self.net.get('ifs', []) if i['n'] in db['ifs']])
            if not nets:
                self.scan_touch(step='nema izabranih mreža')
                return
            # 1) ko je živ: ping svih adresa + ARP tabela (vidi i uređaje koji ne odgovaraju na ping)
            sem = asyncio.Semaphore(48)
            hosts = [(ifc, me, str(h)) for ifc, me, net in nets for h in net.hosts()]
            self.scan_touch(step='tražim uređaje (%d adresa)…' % len(hosts), prog=5)
            alive = set()
            done = 0

            async def one(ifc, me, ip):
                nonlocal done
                if await netscan.ping(ip, sem):
                    alive.add(ip)
                done += 1
                if done % 32 == 0:
                    self.scan_touch(prog=5 + int(40 * done / len(hosts)))
            await asyncio.gather(*(one(*h) for h in hosts))
            macs = {}
            for l in (await self._run('ip', 'neigh', timeout=5)).splitlines():
                m = re.match(r'(\d+\.\d+\.\d+\.\d+) dev (\S+) lladdr (\S+) (\S+)', l)
                if m and m.group(4) not in ('FAILED', 'INCOMPLETE'):
                    ip = m.group(1)
                    if any(ipaddress.ip_address(ip) in net for _, _, net in nets):
                        macs[ip] = m.group(3).lower()
                        alive.add(ip)
            mine = {me: ifc for ifc, me, _ in nets}
            alive |= set(mine)
            ip_if = {h[2]: h[0] for h in hosts}
            ip_if.update(mine)
            # 2) portovi i imena
            alive = sorted(alive, key=lambda x: tuple(int(p) for p in x.split('.')))
            self.scan_touch(step='proveravam portove na %d uređaja…' % len(alive), prog=50)
            psem = asyncio.Semaphore(150)
            ports = {ip: [] for ip in alive}

            async def pchk(ip, port):
                if await netscan.port_open(ip, port, psem):
                    ports[ip].append(port)
            await asyncio.gather(*(pchk(ip, p) for ip in alive for p in netscan.PORTS))
            self.scan_touch(step='tražim imena uređaja…', prog=80)
            loop = asyncio.get_running_loop()
            names = await asyncio.gather(*(loop.run_in_executor(None, netscan.lookup_name, ip, gw) for ip in alive))
            # 3) spoji sa sačuvanim spiskom (ključ: MAC; uređaj može promeniti IP)
            now, first_scan = time.time(), not db['devices']
            for d in db['devices'].values():
                d['online'] = False
                d['new'] = False
            own_mac = {i['n']: i.get('mac') for i in self.net.get('ifs', [])}
            for ip, name in zip(alive, names):
                mac = macs.get(ip) or (own_mac.get(mine[ip]) if ip in mine else None)
                key = mac or 'ip-' + ip
                d = db['devices'].get(key)
                if d is None:
                    d = db['devices'][key] = {'first': now, 'label': ''}
                    d['new'] = not first_scan
                d.update(ip=ip, mac=mac, ifc=ip_if.get(ip), last=now, online=True, ports=sorted(ports[ip]),
                         vendor=netscan.vendor(mac), name=name or d.get('name') or (socket.gethostname() if ip in mine else ''))
                d['type'], d['kind'] = netscan.guess(d, gw, ip if ip in mine else None)
                d['link'] = netscan.link_guess(d)
            for k in [k for k, d in db['devices'].items() if not d.get('online') and not d.get('label') and now - d['last'] > 30 * 86400]:
                del db['devices'][k]   # neimenovani uređaji koji se ne vide mesec dana
            db['last'] = now
            self.scan_save()
            on = sum(d['online'] for d in db['devices'].values())
            new = sum(d.get('new', False) for d in db['devices'].values())
            self.scan_touch(step='%d uređaja na mreži%s' % (on, (' · %d novih' % new) if new else ''), prog=100)
        except Exception:
            log.exception('skeniranje mreže')
            self.scan_touch(step='greška pri skeniranju')
        finally:
            self.scan_touch(running=False)

    # ---------- Telefonska linija (Alati): SLIC (linija za obični telefon: zvono, slušalica, biranje) + GSM modul SIM800.
    # Ovde je podešavanje i provera hardvera; povezivanje (GSM poziv -> zvoni telefon…) koristi ova podešavanja.
    PHONE_DEF = {
        'gsm': {'on': False, 'model': 'A7672E', 'port': '/dev/serial0', 'baud': 115200, 'pwrkey': 0, 'pin': '', 'apn': '', 'audio': 'usb'},
        'slic': {'on': False, 'chip': 'KS0835F', 'rm': 22, 'fr': 23, 'shk': 24, 'shk_low': True, 'ring_hz': 25, 'cadence': 'eu',
                 'dial': 'both', 'tone_hz': 425, 'audio': 'usb'},
        'route': {'gsm_ring': True, 'dial_out': True, 'alarm_call': '', 'alarm_sms': '', 'allow': []},
    }
    PHONE_PORTS = ['/dev/serial0', '/dev/ttyS0', '/dev/ttyAMA0', '/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2']   # modem je na TX/RX ploče (serial0)
    MODEMS = {'A7672E': 'SIMCom A7672E (LTE Cat-1)', 'SIM800': 'SIMCom SIM800 (2G)', 'SIM': 'Simulator (bez modema, za test)'}
    CADENCE = {'eu': [(1.0, 4.0)], 'us': [(2.0, 4.0)], 'uk': [(0.4, 0.2), (0.4, 2.0)], 'short': [(0.4, 0.2), (0.4, 0.2), (0.4, 3.0)]}

    def phone_cfg(self):
        c = json.loads(json.dumps(self.PHONE_DEF))
        for k, v in (self.st.get('phone') or {}).items():
            if isinstance(v, dict) and k in c:
                c[k].update(v)
        c['slic']['chip'] = 'KS0835F'   # jedini SLIC koji se koristi
        # pinovi i port nisu podešavanje sa stranice: proširenje (EXP1/EXP2) se bira, pinovi su u pins.toml
        from pins import PINS
        c['exp'] = (self.st.get('phone') or {}).get('exp') or 'exp1'
        c['slic'].update(PINS.slic(c['exp']))
        c['gsm'].update(port='/dev/serial0', baud=PINS.baud, pwrkey=PINS.pwrkey(c['exp']) or 0)
        c['route'].pop('bell_ring', None)
        return c

    def phone_view(self):
        c = self.phone_cfg()
        st = getattr(self, 'phone_st', None) or {}
        g = {k: v for k, v in c['gsm'].items() if k not in ('port', 'baud', 'pwrkey', 'pin')}   # port i pinovi nisu za stranicu
        g['pin_set'] = bool(c['gsm']['pin'])   # PIN se ne šalje nazad
        slic = {k: v for k, v in c['slic'].items() if k not in ('rm', 'fr', 'shk', 'shk_low')}
        hook = None
        if c['slic']['on']:
            dev = self._phone_in(int(c['slic']['shk']))
            if dev is not None:
                hook = bool(dev.value) != bool(c['slic']['shk_low'])   # True = slušalica podignuta
        hook = self.ph()['hook'] if self.ph().get('sim_hook') is not None else hook
        return {'exp': c['exp'], 'gsm': g, 'slic': slic, 'route': c['route'], 'gsm_st': st.get('gsm'), 'hook': hook,
                'line': self.phone_line_view(), 'calls': self.calls()[-40:][::-1], 'modem_ok': self.mdm_ready(),
                'uart_ok': os.path.exists(c['gsm']['port']), 'scope_exp': self.tool_cfg('scope').get('exp')}

    def _phone_in(self, pin):
        d = getattr(self, '_phone_shk', None)
        if d and d[0] == pin:
            return d[1]
        if d and d[1]:
            d[1].close()
        dev = None
        try:
            from gpiozero import DigitalInputDevice
            dev = DigitalInputDevice(pin, pull_up=True)
            loop = asyncio.get_event_loop()   # slušalica gore/dole: odmah na stranicu
            # slušalica: ivice idu u logiku linije (podizanje, spuštanje, impulsno biranje)
            dev.when_activated = lambda: loop.call_soon_threadsafe(self._shk_edge_dev)
            dev.when_deactivated = lambda: loop.call_soon_threadsafe(self._shk_edge_dev)
        except Exception as e:
            log.warning('SLIC SHK na GPIO%s nije dostupan: %s', pin, e)
        self._phone_shk = (pin, dev)
        return dev

    def _shk_edge_dev(self):
        if self.ph().get('sim_hook') is None:
            self.ph_shk_edge(self._shk_now(), time.monotonic())

    def phone_set(self, a):
        c = self.phone_cfg()
        try:
            if a.get('exp') in ('exp1', 'exp2'):
                if a['exp'] == self.tool_cfg('scope').get('exp'):
                    return {'err': 'Na %s je već Osciloskop.' % a['exp'].upper()}
                c['exp'] = a['exp']
            g = a.get('gsm') or {}
            if 'on' in g:
                c['gsm']['on'] = bool(g['on'])
            if g.get('model') in self.MODEMS:
                c['gsm']['model'] = g['model']
            if g.get('audio') in ('usb', 'spi', 'none'):
                c['gsm']['audio'] = g['audio']
            if 'pin' in g:
                c['gsm']['pin'] = re.sub(r'\D', '', str(g['pin']))[:8]
            if 'apn' in g:
                c['gsm']['apn'] = re.sub(r'[^\w.\-]', '', str(g['apn']))[:40]
            s = a.get('slic') or {}
            if 'on' in s:
                c['slic']['on'] = bool(s['on'])
            if 'ring_hz' in s and int(s['ring_hz']) in (20, 25, 50):
                c['slic']['ring_hz'] = int(s['ring_hz'])
            if s.get('cadence') in self.CADENCE:
                c['slic']['cadence'] = s['cadence']
            if s.get('dial') in ('dtmf', 'pulse', 'both'):
                c['slic']['dial'] = s['dial']
            if 'tone_hz' in s and int(s['tone_hz']) in (350, 400, 425, 440, 450):
                c['slic']['tone_hz'] = int(s['tone_hz'])
            if s.get('audio') in ('usb', 'hdmi', 'jack', 'i2s'):
                c['slic']['audio'] = s['audio']
            r = a.get('route') or {}
            for k in ('gsm_ring', 'dial_out'):
                if k in r:
                    c['route'][k] = bool(r[k])
            for k in ('alarm_call', 'alarm_sms'):
                if k in r:
                    c['route'][k] = re.sub(r'[^\d+]', '', str(r[k]))[:16]
            if 'allow' in r:
                c['route']['allow'] = [x for x in (re.sub(r'[^\d+]', '', str(n))[:16] for n in (r['allow'] or [])[:20]) if x]
        except (TypeError, ValueError):
            return {'err': 'Neispravna vrednost.'}
        for k in ('rm', 'fr', 'shk', 'shk_low'):   # pinovi se ne čuvaju u stanju (pins.toml)
            c['slic'].pop(k, None)
        for k in ('port', 'baud', 'pwrkey'):
            c['gsm'].pop(k, None)
        self.st['phone'] = c
        save_state(self.st)
        self.sms_kick()   # modem: otvori / zatvori / ponovo poveži odmah
        self.mark()
        return {}

    # ---------- modem: stalna veza (modem.Link) — odmah hvata pozive (RING/+CLIP/+CLCC) i poruke (+CMTI).
    # AT komande idu jedna po jedna (gsm_lock + nit). Model 'SIM' = simulator modema (test bez hardvera).
    def mdm_ready(self):
        lk = getattr(self, 'mdm', None)
        return bool(lk and lk.alive and getattr(self, 'mdm_ok', False))

    async def mdm_ensure(self):
        """Otvori vezu kad je modem uključen (i port postoji), zatvori kad nije; ponovo otvori kad se promeni model/brzina."""
        import modem
        g = self.phone_cfg()['gsm']
        sim = g['model'] == 'SIM'
        want = g['on'] and (sim or os.path.exists(g['port']))
        key = (g['model'], g['baud'], g['pin'])
        lk = getattr(self, 'mdm', None)
        if lk and (not want or not lk.alive or getattr(self, 'mdm_key', None) != key):
            lk.close()
            self.mdm, self.mdm_ok = None, False
            self.sms_st = dict(getattr(self, 'sms_st', None) or {}, st=None if not want else 'err', err=None if not want else str(lk.err or 'veza prekinuta'))
            self.mark()
            lk = None
        self.ph_slic_ensure()
        if not want or lk:
            return
        loop = asyncio.get_running_loop()
        self.loop = loop
        try:
            lk = await loop.run_in_executor(None, lambda: modem.Link(g['port'], g['baud'], self._mdm_urc, sim=sim))
            self.mdm, self.mdm_key, self.mdm_ok = lk, key, False
            self.mdm_since = time.time()
            async with self.gsm_lock():
                await loop.run_in_executor(None, modem.setup, lk, g['model'], g['pin'])
            self.mdm_ok = True
            log.info('modem %s povezan%s', g['model'], ' (simulator)' if sim else '')
            self.sms_st = dict(getattr(self, 'sms_st', None) or {}, st='ok', err=None, t=time.time())
        except Exception as e:
            log.warning('modem: %s', e)
            if getattr(self, 'mdm', None):
                self.mdm.close()
            self.mdm, self.mdm_ok = None, False
            self.sms_st = dict(getattr(self, 'sms_st', None) or {}, st='err', err=str(e)[:120], t=time.time())
        self.mark()

    async def mdm_call(self, fn, *args):
        """modem.<fn>(link, *args) u niti, jedna po jedna."""
        if not getattr(self, 'mdm', None):
            raise OSError('modem nije povezan')
        async with self.gsm_lock():
            return await asyncio.get_running_loop().run_in_executor(None, fn, self.mdm, *args)

    async def mdm_at(self, cmd, wait=5):
        try:
            return await self.mdm_call(lambda lk: lk.at(cmd, wait))
        except Exception as e:
            log.warning('modem %s: %s', cmd, e)
            return 'ERROR'

    def _mdm_urc(self, line):   # iz niti modema
        loop = getattr(self, 'loop', None)
        if loop:
            loop.call_soon_threadsafe(self._urc, line)

    def _urc(self, line):
        if line != 'RING' and not line.startswith('+CLIP'):
            log.info('modem: %s', line[:80])
        if line == '__closed__':
            self.mdm_ok = False
            self.sms_kick()
        elif line == 'RING':
            self.ph_incoming(None)
        elif line.startswith('+CLIP:'):
            m = re.search(r'\+CLIP: "([^"]*)"', line)
            self.ph_incoming(m.group(1) if m else '')
        elif line.startswith('+CLCC:'):
            m = re.match(r'\+CLCC: \d+,(\d),(\d)(?:,\d+,\d+,"([^"]*)")?', line)
            if m:
                d, st, num = m.group(1), m.group(2), m.group(3) or ''
                if st == '4' and d == '1':
                    self.ph_incoming(num)
                elif st == '0':
                    self.ph_connected()
                elif st == '6':
                    self.ph_remote_end()
        elif line.startswith('VOICE CALL: BEGIN'):
            self.ph_connected()
        elif line.startswith(('NO CARRIER', 'BUSY', 'NO ANSWER', 'VOICE CALL: END', 'MISSED_CALL')):
            self.ph_remote_end(gsm=True)
        elif line.startswith('+CMTI'):
            self.sms_kick()

    # ---------- linija: stanje poziva. idle -> ring_in (zvoni) -> talk; idle -> dial (ton, biranje) -> calling -> talk;
    # druga strana spusti -> ended (zauzeće dok se slušalica ne spusti); alarm -> auto (poziv bez slušalice)
    CALLS_FILE = DATA / 'calls.json'

    def ph(self):
        if getattr(self, '_ph', None) is None:
            self._ph = {'state': 'idle', 'num': '', 'digits': '', 't': time.time(), 'since': None, 'hook': False, 'dir': ''}
        return self._ph

    def ph_set(self, state, **kw):
        p = self.ph()
        if p['state'] != state:
            log.info('linija: %s -> %s %s', p['state'], state, kw.get('num') or p.get('num') or '')
        p.update(state=state, t=time.time(), **kw)
        self.mark()

    def calls(self):
        if getattr(self, '_calls', None) is None:
            try:
                self._calls = json.loads(self.CALLS_FILE.read_text())
            except (OSError, ValueError):
                self._calls = []
        return self._calls

    def call_log(self, kind, num, dur=0):
        c = self.calls()
        c.append({'t': time.time(), 'dir': kind, 'num': num or '', 'dur': int(dur)})
        del c[:-300]
        tmp = self.CALLS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(c, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.CALLS_FILE)

    def ph_audio(self):
        import phoneline
        kind = self.phone_cfg()['slic']['audio']
        c = getattr(self, '_ph_audio_c', None)
        if not c or c[0] != kind or time.time() - c[2] > 30:   # /proc/asound/cards se čita najviše na 30 s
            c = self._ph_audio_c = (kind, phoneline.alsa_device(kind), time.time())
        return c[1]

    def ph_tone(self, kind):
        import phoneline
        if not hasattr(self, '_tones'):
            self._tones = phoneline.Tones()
        s = self.phone_cfg()['slic']
        if kind and s['on']:
            self._tones.play(kind, self.ph_audio(), s['tone_hz'])
        else:
            self._tones.stop()

    def ph_dtmf(self, on):
        import phoneline
        if not hasattr(self, '_dtmf'):
            self._dtmf = phoneline.Dtmf()
        s = self.phone_cfg()['slic']
        if on and s['on'] and s['dial'] in ('dtmf', 'both'):
            loop = asyncio.get_running_loop()
            self._dtmf.start(self.ph_audio(), lambda k: loop.call_soon_threadsafe(self.ph_digit, k))
        else:
            self._dtmf.stop()

    def ph_ring(self, on):
        import phoneline
        if not hasattr(self, '_ringer'):
            self._ringer = phoneline.Ringer()
        s = self.phone_cfg()['slic']
        if on and s['on']:
            try:
                self._ringer.start(s['rm'], s['fr'], s['ring_hz'], self.CADENCE[s['cadence']])
            except Exception as e:
                log.warning('zvono: %s', e)
        else:
            self._ringer.stop()
        self.mark()

    def ph_slic_ensure(self):
        """Ulaz SHK (slušalica) postoji dok je SLIC uključen."""
        s = self.phone_cfg()['slic']
        if s['on']:
            self._phone_in(int(s['shk']))

    def ph_incoming(self, num, direct=False):
        if not direct and self.pbx_on():
            return self.pbx_gsm_ring(num)   # centrala: poziv ide kroz najavu i krugove zvonjenja
        p, r = self.ph(), self.phone_cfg()['route']
        if num is not None and num != '':
            num = self.sms_num(num) or num
        if p['state'] == 'ring_in':
            p['last_ring'] = time.time()
            if num and not p['num']:
                p['num'] = num
                self.ph_allowed_check()
            return
        if p['state'] != 'idle':
            return   # zauzeto (u razgovoru / bira): modem sam javlja zauzeće ili poziv na čekanju
        self.ph_set('ring_in', num=num or '', dir='in', digits='', last_ring=time.time())
        if num:
            if not self.ph_allowed_check():
                return
        if r['gsm_ring']:
            self.ph_ring(True)
        asyncio.get_running_loop().create_task(self.ph_ring_watch())

    def ph_allowed_check(self):
        p, allow = self.ph(), self.phone_cfg()['route']['allow']
        if allow and p['num'] and p['num'] not in [self.sms_num(x) for x in allow]:
            log.info('poziv od %s odbijen (nije na spisku dozvoljenih)', p['num'])
            self.ph_ring(False)
            self.call_log('rejected', p['num'])
            self.ph_set('idle', num='')
            asyncio.get_running_loop().create_task(self.mdm_at('ATH'))
            return False
        return True

    async def ph_ring_watch(self):
        """Pozivalac odustao: modem ne javi kraj uvek, ali RING prestane (posle ~8 s)."""
        while self.ph()['state'] == 'ring_in':
            await asyncio.sleep(1)
            if time.time() - self.ph().get('last_ring', 0) > 8 and self.ph()['state'] == 'ring_in':
                self.ph_remote_end()

    def ph_shk_edge(self, lifted, t):
        """Ivica SHK: pravo podizanje/spuštanje, impulsno biranje (prekidi 30–150 ms) i kratko spuštanje (flash) se razlikuju."""
        p = self.ph()
        if not lifted:
            p['down_t'] = t
            seq = p['down_seq'] = p.get('down_seq', 0) + 1

            async def later():
                await asyncio.sleep(0.6)
                if self.ph().get('down_seq') == seq and not self._shk_now():
                    self.ph_hook(False)
            asyncio.get_running_loop().create_task(later())
            return
        dur = t - p.get('down_t', 0)
        if not p['hook']:
            self.ph_hook(True)
        elif 0.03 <= dur <= 0.15 and p['state'] == 'dial' and self.phone_cfg()['slic']['dial'] in ('pulse', 'both'):
            p['pulses'] = p.get('pulses', 0) + 1
            seq = p['pulse_seq'] = p.get('pulse_seq', 0) + 1

            async def digit():
                await asyncio.sleep(0.4)
                if self.ph().get('pulse_seq') == seq and self.ph().get('pulses'):
                    n = self.ph().pop('pulses')
                    self.ph_digit('0' if n >= 10 else str(n))
            asyncio.get_running_loop().create_task(digit())

    def _shk_now(self):
        if self.ph().get('sim_hook') is not None:
            return self.ph()['sim_hook']
        s = self.phone_cfg()['slic']
        dev = self._phone_in(int(s['shk']))
        return dev is not None and bool(dev.value) != bool(s['shk_low'])

    def ph_hook(self, up):
        p, r = self.ph(), self.phone_cfg()['route']
        if p['hook'] == up:
            return
        p['hook'] = up
        loop = asyncio.get_running_loop()
        if up:
            if p['state'] == 'auto':   # alarm poziv u toku: prekini ga, korisnik preuzima liniju
                loop.create_task(self.mdm_at('ATH'))
                self.ph_set('idle')
            if p['state'] == 'ring_in' and p.get('pbx'):   # poziv sa centrale: javi se i uključi zvuk SLIC-a u most
                self.ph_ring(False)
                self.ph_set('talk', since=time.time())
                cid = p['pbx']

                async def answer():
                    await self.pbx_req('POST', '/channels/%s/answer' % cid)
                    await self.pbx_bridge(cid, self.ph_audio())
                loop.create_task(answer())
            elif p['state'] == 'ring_in':
                self.ph_ring(False)
                self.ph_set('talk', since=time.time())
                loop.create_task(self.ph_answer())
            elif p['state'] == 'idle':
                pbx_ok = self.pbx_on() and getattr(self, 'pbx_ari', None) and self.pbx_ari.ok
                if pbx_ok or (r['dial_out'] and self.mdm_ready()):
                    self.ph_set('dial', digits='', num='', dir='out')
                    self.ph_tone('dial')
                    self.ph_dtmf(True)
                    loop.create_task(self.ph_dial_timer(p['t']))
                else:
                    self.ph_set('ended', note='modem nije spreman' if r['dial_out'] else 'odlazni pozivi isključeni')
                    self.ph_tone('busy')
        else:
            if p.get('pbx'):   # poziv kroz centralu: prekini kanal
                cid = p['pbx']
                p['pbx'] = None
                if p['state'] == 'talk':
                    self.ph_log_end()

                async def drop():
                    await self.pbx_unbridge(cid)
                    await self.pbx_hangup(cid)
                loop.create_task(drop())
            elif p['state'] in ('talk', 'calling'):
                loop.create_task(self.mdm_at('ATH'))
                self.ph_log_end()
            self.ph_tone(None)
            self.ph_dtmf(False)
            self.ph_set('idle', num='', digits='', since=None, note='')
        self.mark()

    async def ph_answer(self):
        r = await self.mdm_at('ATA', 10)
        if 'OK' not in r and self.ph()['state'] == 'talk' and 'BEGIN' not in r:
            log.warning('javljanje nije uspelo: %s', r.strip()[:60])

    async def ph_dial_timer(self, t0):
        """Biranje: posle 4 s bez nove cifre -> poziv; bez ijedne cifre 30 s -> zauzeće."""
        while self.ph()['state'] == 'dial':
            await asyncio.sleep(0.5)
            p = self.ph()
            if p['state'] != 'dial':
                return
            if p['digits'] and time.time() - p.get('last_digit', 0) > 4:
                return await self.ph_dial_now()
            if not p['digits'] and time.time() - t0 > 30:
                self.ph_dtmf(False)
                self.ph_set('ended', note='nije birano')
                self.ph_tone('busy')
                return

    def ph_digit(self, d):
        p = self.ph()
        if p['state'] == 'dial':
            if not p['digits']:
                self.ph_tone(None)   # ton slobodne linije prestaje na prvu cifru
            if d == '#' and p['digits']:
                asyncio.get_running_loop().create_task(self.ph_dial_now())
                return
            p['digits'] = (p['digits'] + d)[:20]
            p['last_digit'] = time.time()
            self.mark()
            if self.pbx_on() and (p['digits'] in self.pbx_cfg()['ext'] or p['digits'] == '*97'):   # lokal / pošta: biraj odmah
                asyncio.get_running_loop().create_task(self.ph_dial_now())
        elif p['state'] == 'talk' and p.get('pbx') and d in '0123456789*#':   # kroz centralu: taster ide u Asterisk (pošta, meni)
            asyncio.get_running_loop().create_task(self.pbx_req('POST', '/channels/%s/dtmf' % p['pbx'], dtmf=d))
        elif p['state'] == 'talk' and d in '0123456789*#ABCD':   # u razgovoru: tonovi ka drugoj strani (meni, govorna pošta)
            asyncio.get_running_loop().create_task(self.mdm_at('AT+VTS=' + d))

    async def ph_dial_now(self):
        p = self.ph()
        num = p['digits'].rstrip('#')
        self.ph_dtmf(False)
        if len(num) < 3:
            self.ph_set('ended', note='prekratak broj')
            self.ph_tone('busy')
            return
        if self.pbx_on() and getattr(self, 'pbx_ari', None) and self.pbx_ari.ok:   # kroz centralu: lokal ili spolja preko modema
            c = self.pbx_cfg()
            self.ph_set('calling', num=num, since=None)
            self.ph_tone('ringback')
            se = c.get('slic_ext') or ''
            ch = await self.pbx_req('POST', '/channels', endpoint='Local/%s@teco-internal' % num, app='teco', appArgs='slic-out',
                                    callerId='"%s" <%s>' % ((c['ext'].get(se) or {}).get('name', 'Telefon'), se or 'fxs'), timeout=60)
            if ch and self.ph()['state'] == 'calling':
                self.ph()['pbx'] = ch['id']
            elif self.ph()['state'] == 'calling':
                self.ph_set('ended', note='centrala nije prihvatila poziv')
                self.ph_tone('busy')
            return
        self.ph_set('calling', num=self.sms_num(num) if not num.startswith(('*', '#')) else num, since=None)
        r = await self.mdm_at('ATD%s;' % num, 15)
        if 'OK' not in r and self.ph()['state'] == 'calling':
            self.call_log('out', self.ph()['num'])
            self.ph_set('ended', note='poziv nije uspostavljen')
            self.ph_tone('busy')

    def ph_connected(self):
        if getattr(self, 'gc', None):
            return self.pbx_gsm_connected()
        p = self.ph()
        if p['state'] == 'calling':
            self.ph_set('talk', since=time.time())
        elif p['state'] == 'auto':
            p['since'] = time.time()

    def ph_log_end(self):
        p = self.ph()
        if self.ph_pbx_ext_only(p):
            return
        kind = 'in' if p.get('dir') == 'in' else 'out'
        self.call_log(kind, p['num'], time.time() - p['since'] if p.get('since') else 0)

    def ph_pbx_ext_only(self, p):
        """Kroz centralu SLIC upisuje samo razgovore sa lokalima — spoljni (mobilni) deo upisuje GSM strana (inače dupli zapis)."""
        return bool(p.get('pbx') or (self.pbx_on() and p.get('state') in ('talk', 'calling', 'ring_in'))) and p.get('num') not in self.pbx_cfg()['ext']

    def ph_remote_end(self, gsm=False):
        if getattr(self, 'gc', None):
            asyncio.get_running_loop().create_task(self.pbx_gsm_end())
            return
        p = self.ph()
        if p.get('pbx'):   # SLIC je u pozivu kroz centralu: kraj javlja Asterisk, ne modem
            return
        if p['state'] == 'ring_in':
            self.ph_ring(False)
            self.call_log('missed', p['num'])
            self.ph_set('idle', num='')
            self.notify_missed(p['num'])
        elif p['state'] in ('calling', 'talk'):
            self.ph_log_end()
            self.ph_set('ended', note='druga strana je spustila' if p['state'] == 'talk' else 'zauzeto / nema odgovora')
            self.ph_tone('busy')
        elif p['state'] == 'auto':
            self.call_log('alarm', p['num'], time.time() - p['since'] if p.get('since') else 0)
            self.ph_set('idle', num='')

    def notify_missed(self, num):
        """Propušten poziv dok niko nije kod kuće: zapis + (ako je uključeno) SMS pouzdanim brojevima ide preko obaveštenja."""
        log.info('propušten poziv od %s', num or 'nepoznat broj')

    async def ph_auto_call(self, num, hold=45):
        """Alarm: pozovi broj (bez slušalice), drži do 45 s pa spusti — telefon zazvoni vlasniku."""
        p = self.ph()
        if p['state'] != 'idle' or not self.mdm_ready() or not num:
            return
        self.ph_set('auto', num=self.sms_num(num), dir='out', since=None)
        await self.mdm_at('ATD%s;' % num, 15)
        t0 = time.time()
        while self.ph()['state'] == 'auto' and time.time() - t0 < hold:
            await asyncio.sleep(1)
        if self.ph()['state'] == 'auto':
            await self.mdm_at('ATH')
            self.ph_remote_end()

    def phone_line_view(self):
        p = self.ph()
        return {'state': p['state'], 'num': p['num'], 'digits': p['digits'], 'since': p.get('since'), 't': p['t'], 'note': p.get('note', ''),
                'hook': p['hook'], 'ringing': bool(getattr(self, '_ringer', None) and self._ringer.ringing),
                'tone': getattr(getattr(self, '_tones', None), 'kind', None), 'audio': self.ph_audio()}

    async def phone_test(self, a):
        import modem
        kind = a.get('kind')
        c = self.phone_cfg()
        self.phone_st = getattr(self, 'phone_st', None) or {}
        if kind == 'gsm':
            if not c['gsm']['on']:
                return {'err': 'Uključi modem.'}
            await self.mdm_ensure()
            if not self.mdm_ready():
                err = (getattr(self, 'sms_st', None) or {}).get('err') or 'modem ne odgovara'
                self.phone_st['gsm'] = {'ok': False, 'err': err, 't': time.time()}
                self.mark()
                return {'err': 'Modem: %s' % err}
            try:
                self.phone_st['gsm'] = await self.mdm_call(modem.info, c['gsm']['model'])
            except Exception as e:
                self.phone_st['gsm'] = {'ok': False, 'err': str(e)[:120], 't': time.time()}
                self.mark()
                return {'err': 'Modem: %s' % e}
            self.mark()
            return {'ok': 'Modem odgovara'}
        if kind == 'sms':
            return await self.sms_cmd('sms_send', {'to': a.get('to'), 'text': 'Teco.Pi: test poruka'})
        if kind == 'ring':
            if not c['slic']['on']:
                return {'err': 'Uključi SLIC liniju.'}
            if self.ph()['state'] != 'idle':
                return {'err': 'Linija je zauzeta.'}
            self.ph_ring(True)

            async def off():
                await asyncio.sleep(6)
                if self.ph()['state'] == 'idle':
                    self.ph_ring(False)
            asyncio.get_running_loop().create_task(off())
            return {'ok': 'Zvoni 6 s'}
        return {'err': 'Nepoznat test.'}

    async def phone_sim(self, a):
        """Simulacija sa stranice: modem (model 'SIM') i slušalica — test cele linije bez hardvera."""
        act = a.get('act')
        lk = getattr(self, 'mdm', None)
        sim = lk.ser if lk and getattr(lk, 'sim', False) else None
        if act in ('hook_up', 'hook_down'):
            self.ph()['sim_hook'] = act == 'hook_up'
            self.ph_hook(act == 'hook_up')
            return {}
        if act == 'digits':
            for d in str(a.get('digits') or '')[:20]:
                if d in '0123456789*#':
                    self.ph_digit(d)
                    await asyncio.sleep(0.05)
            return {}
        if act == 'hook_release':   # vrati čitanje slušalice na pravi SHK ulaz
            self.ph().pop('sim_hook', None)
            return {}
        if not sim:
            return {'err': 'Izaberi model „Simulator“ i uključi modem.'}
        if act == 'incoming':
            num = self.sms_num(a.get('num') or '') or '+381641234567'
            sim.sim_incoming(num)
        elif act == 'hangup':
            sim.sim_hangup()
        elif act == 'answer':
            sim.sim_answer()
        elif act == 'sms':
            sim.sim_sms(self.sms_num(a.get('num') or '') or '+381641234567', str(a.get('text') or 'STATUS')[:300])
        else:
            return {'err': 'Nepoznata simulacija.'}
        return {}

    # ---------- SMS poruke (Alati -> Komunikacija): preko SIM800 iz Telefonske linije. Istorija: data/sms.json.
    # Primljene se čitaju sa SIM kartice na 20 s (i brišu sa nje), poslate idu iz reda (čekaju dok modul ne proradi).
    SMS_FILE = DATA / 'sms.json'

    def gsm_lock(self):
        if getattr(self, '_gsm_lock', None) is None:
            self._gsm_lock = asyncio.Lock()   # port modula koristi jedan po jedan (SMS, provera modula)
        return self._gsm_lock

    def sms_db(self):
        if getattr(self, '_sms', None) is None:
            try:
                self._sms = json.loads(self.SMS_FILE.read_text())
            except (OSError, ValueError):
                self._sms = {'msgs': [], 'names': {}, 'seq': 0}
        return self._sms

    def sms_save(self):
        db = self.sms_db()
        db['msgs'] = db['msgs'][-5000:]
        tmp = self.SMS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(db, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.SMS_FILE)

    @staticmethod
    def sms_num(n):
        n = re.sub(r'[^\d+]', '', str(n or ''))[:16]
        if n.startswith('00'):
            n = '+' + n[2:]
        elif n.startswith('0'):   # domaći broj (064…) -> +38164…
            n = '+381' + n[1:]
        return n

    def sms_view(self):
        db = self.sms_db()
        convs = {}
        for m in db['msgs']:
            c = convs.setdefault(m['num'], {'num': m['num'], 'n': 0, 'unread': 0})
            c['n'] += 1
            c['t'], c['last'], c['dir'], c['st'] = m['t'], m['text'][:80], m['dir'], m.get('st')
            if m['dir'] == 'in' and not m.get('read'):
                c['unread'] += 1
        for c in convs.values():
            c['name'] = db['names'].get(c['num'], '')
        st = getattr(self, 'sms_st', None) or {}
        g = self.phone_cfg()['gsm']
        return {'convs': sorted(convs.values(), key=lambda c: -c['t']), 'unread': sum(c['unread'] for c in convs.values()),
                'on': g['on'], 'port_ok': g['model'] == 'SIM' or os.path.exists(g['port']), 'st': st.get('st'), 'err': st.get('err'), 'poll': st.get('t'), 'csq': st.get('csq'),
                'queued': sum(1 for m in db['msgs'] if m.get('st') == 'queued'),
                'notify': dict(self.notify_cfg(), pin='', pin_set=bool(self.notify_cfg()['pin'])), 'ussd': st.get('ussd')}

    def sms_add(self, **m):
        db = self.sms_db()
        db['seq'] = db.get('seq', 0) + 1
        m['id'] = db['seq']
        db['msgs'].append(m)
        return m

    async def sms_loop(self):
        """Veza sa modemom (otvori / zatvori po podešavanju) + na 60 s čitanje poruka i slanje reda.
        Nova poruka (+CMTI) i slanje sa stranice ne čekaju 60 s (sms_kick)."""
        self._sms_kick = asyncio.Event()
        self.loop = asyncio.get_running_loop()   # za događaje iz niti (modem, ulazi)
        while True:
            try:
                await self.mdm_ensure()
                if self.mdm_ready():
                    await self.sms_cycle()
            except Exception:
                log.exception('modem / SMS')
            try:
                await asyncio.wait_for(self._sms_kick.wait(), 60 if self.mdm_ready() else 10)
            except asyncio.TimeoutError:
                pass
            self._sms_kick.clear()

    def sms_kick(self, g=None):
        if getattr(self, '_sms_kick', None):
            self._sms_kick.set()

    async def sms_cycle(self, g=None):
        import modem
        self.sms_st = getattr(self, 'sms_st', None) or {}
        db = self.sms_db()
        try:
            msgs, csq = await self.mdm_call(modem.poll_sms)
            self.sms_st.update(st='ok', err=None, t=time.time(), csq=csq)
        except Exception as e:
            self.sms_st.update(st='err', err=str(e)[:120], t=time.time())
            self.mark()
            return
        for x in msgs:
            num = self.sms_num(x['num']) or x['num']
            self.sms_add(num=num, dir='in', text=x['text'], t=x['t'], read=False)
            log.info('SMS primljen od %s', x['num'])
            asyncio.get_running_loop().create_task(self.sms_command(num, x['text']))
        for m in [m for m in db['msgs'] if m.get('st') == 'queued']:   # red za slanje
            if self.ph()['state'] in ('ring_in', 'calling'):   # ne zauzimaj modem dok zvoni / bira
                break
            sim = getattr(self.mdm, 'sim', False)
            if sim and m['t'] < getattr(self, 'mdm_since', 0):   # simulator ne šalje prave poruke koje su čekale u redu
                continue
            try:
                m['parts'] = await self.mdm_call(modem.send_sms, m['num'], m['text'])
                m['st'], m['t'] = 'sent', time.time()
                if sim:
                    m['sim'] = True
            except Exception as e:
                m['tries'] = m.get('tries', 0) + 1
                m['err'] = str(e)[:120]
                if m['tries'] >= 3:
                    m['st'] = 'fail'
        if msgs or any(m.get('st') in ('sent', 'fail') and time.time() - m['t'] < 120 for m in db['msgs'][-20:]):
            self.sms_save()
        self.mark()

    async def sms_cmd(self, c, a):
        db = self.sms_db()
        num = self.sms_num(a.get('num') or a.get('to'))
        if c == 'sms_send':
            text = str(a.get('text') or '').strip()[:1000]
            if len(num) < 6 or not text:
                return {'err': 'Upiši broj i poruku.'}
            m = self.sms_add(num=num, dir='out', text=text, t=time.time(), st='queued')
            self.sms_save()
            self.sms_kick()   # pošalji odmah (ne čekaj sledeći krug)
            self.mark()
            return {'ok': 'Šaljem…' if self.mdm_ready() else 'Poruka čeka u redu (modem nije spreman)', 'id': m['id']}
        if c == 'sms_thread':
            return {'num': num, 'name': db['names'].get(num, ''), 'msgs': [m for m in db['msgs'] if m['num'] == num][-500:]}
        if c == 'sms_read':
            for m in db['msgs']:
                if m['num'] == num and m['dir'] == 'in':
                    m['read'] = True
        elif c == 'sms_del':
            db['msgs'] = [m for m in db['msgs'] if m['num'] != num]
        elif c == 'sms_name':
            name = str(a.get('name') or '').strip()[:40]
            if name:
                db['names'][num] = name
            else:
                db['names'].pop(num, None)
        elif c == 'sms_retry':
            for m in db['msgs']:
                if m['id'] == a.get('id') and m.get('st') == 'fail':
                    m.update(st='queued', tries=0)
        self.sms_save()
        self.mark()
        return {}

    # ---------- SMS obaveštenja (alarm, struja, akvarijum, uređaji sa mreže) i SMS komande sa pouzdanih brojeva
    NOTIFY_DEF = {'nums': [], 'alarm': True, 'arm': False, 'power': True, 'aqua': True, 'devices': True, 'web': True, 'notes': True, 'cmds': True, 'pin': ''}

    def notify_cfg(self):
        return dict(self.NOTIFY_DEF, **(self.st.get('notify') or {}))

    def notify_set(self, a):
        c = self.notify_cfg()
        for k in ('alarm', 'arm', 'power', 'aqua', 'devices', 'web', 'notes', 'cmds'):
            if k in a:
                c[k] = bool(a[k])
        if 'nums' in a and isinstance(a['nums'], list):
            c['nums'] = [n for n in dict.fromkeys(self.sms_num(x) for x in a['nums'][:10]) if len(n) >= 8]
        if 'pin' in a:
            c['pin'] = re.sub(r'\D', '', str(a['pin']))[:8]
        self.st['notify'] = c
        save_state(self.st)
        self.mark()
        return {}

    def notify(self, kind, text, key=None):
        """Obaveštenje SMS-om (pouzdani brojevi) i e-mailom (Ekstenzije → Email); isto najviše jednom u 10 min."""
        c = self.notify_cfg()
        m = self.mail_cfg()
        mail = m.get('on') and m.get('notify') and m.get('to') and m.get('host')
        if not c.get(kind) or not (c['nums'] or mail):
            return
        last = getattr(self, '_notify_last', None) or {}
        self._notify_last = last
        k = key or (kind + ':' + text)
        if time.time() - last.get(k, 0) < 600:
            return
        last[k] = time.time()
        if mail:
            coro = self.mail_send('Teco.Pi: ' + text[:90], text + '\n\n— Teco.Pi, ' + time.strftime('%d.%m.%Y %H:%M'))
            try:
                asyncio.get_running_loop().create_task(coro)
            except RuntimeError:   # poziv iz GPIO niti (ulazi struja/baterija): na glavnu petlju
                asyncio.run_coroutine_threadsafe(coro, self.loop)
        for n in c['nums']:
            self.sms_add(num=n, dir='out', text='Teco.Pi: ' + text, t=time.time(), st='queued', auto=kind)
        self.sms_save()
        log.info('SMS obaveštenje (%s): %s', kind, text)
        self.sms_kick()   # pošalji odmah (ne čekaj sledeći krug)
        self.mark()

    # ---------- Beleške / kontrolna lista (Alati → Opšte): zajedničke za sve uređaje (data/notes.json), podsetnik u zadato vreme
    NOTES = DATA / 'notes.json'

    def notes_db(self):
        if not hasattr(self, '_notes'):
            try:
                self._notes = json.loads(self.NOTES.read_text())
            except (OSError, ValueError):
                self._notes = []
        return self._notes

    def notes_save(self):
        tmp = self.NOTES.with_suffix('.tmp')
        tmp.write_text(json.dumps(self._notes, ensure_ascii=False))
        os.replace(tmp, self.NOTES)

    def notes_cmd(self, c, a):
        db = self.notes_db()
        if c == 'note_add':
            if len(db) >= 200:
                return {'err': 'Najviše 200 beležaka.'}
            n = {'id': secrets.token_hex(4), 'title': str(a.get('title') or '').strip()[:80], 'text': '', 'items': [], 'remind': None, 'fired': False,
                 'pin': False, 'color': '', 't': time.time()}
            db.insert(0, n)
            self.notes_save()
            self.mark()
            return {'id': n['id']}
        n = next((x for x in db if x['id'] == a.get('id')), None)
        if not n:
            return {'err': 'Beleška ne postoji (možda je obrisana na drugom uređaju).'}
        if c == 'note_del':
            db.remove(n)
        else:   # note_set: samo poslata polja
            if 'title' in a:
                n['title'] = str(a['title'])[:80]
            if 'text' in a:
                n['text'] = str(a['text'])[:5000]
            if isinstance(a.get('items'), list):
                n['items'] = [{'t': str(i.get('t', ''))[:200], 'done': bool(i.get('done'))} for i in a['items'][:100] if isinstance(i, dict)]
            if 'pin' in a:
                n['pin'] = bool(a['pin'])
            if a.get('color') in ('', 'y', 'g', 'b', 'r', 'v'):
                n['color'] = a['color']
            if 'remind' in a:   # unix vreme (s) ili null
                try:
                    n['remind'] = int(a['remind']) if a['remind'] else None
                except (TypeError, ValueError):
                    return {'err': 'Neispravno vreme podsetnika.'}
                n['fired'] = False
            n['t'] = time.time()
        self.notes_save()
        self.mark()
        return {}

    async def notes_loop(self):
        """Na 20 s: podsetnici koji su stigli -> SMS/e-mail (Obaveštenja → Beleške) i prozorčić na svim otvorenim stranicama."""
        while True:
            await asyncio.sleep(20)
            try:
                now, hit = time.time(), []
                for n in self.notes_db():
                    if n.get('remind') and not n.get('fired') and n['remind'] <= now:
                        n['fired'] = True
                        hit.append(n)
                for n in hit:
                    left = [i['t'] for i in n.get('items', []) if not i.get('done')]
                    txt = 'Podsetnik: ' + (n.get('title') or (n.get('text') or '').split('\n')[0][:60] or 'beleška') + (' (' + ', '.join(left[:5]) + ')' if left else '')
                    self.notify('notes', txt, key='note:%s:%s' % (n['id'], n['remind']))
                    await self.push({'remind': {'id': n['id'], 'title': n.get('title') or 'Beleška', 'text': txt}})
                    log.info('beleška: %s', txt)
                if hit:
                    self.notes_save()
                    self.mark()
            except Exception:
                log.exception('beleške')

    def notify_alarm(self, st, aid='a1'):
        """Iz stanja alarm centrale: alarm (zone), uključen/isključen, nestanak struje (posebno za svaku centralu)."""
        parts = st.get('partitions', [])
        al = [p for p in parts if any(p.get(k) for k in ('alarm', 'audible_alarm', 'silent_alarm', 'fire_alarm', 'panic_alarm'))]
        armed = {p.get('id'): bool(p.get('arm')) for p in parts}
        tr = (st.get('system') or {}).get('troubles') or {}
        ac = any(v is True and (k.startswith('ac_') or 'ac_fail' in k or 'ac_loss' in k) for k, v in tr.items())
        prevs = self.__dict__.setdefault('_ntf_prevs', {})
        prev = prevs.get(aid)
        prevs[aid] = {'al': bool(al), 'armed': armed, 'ac': ac}
        pre = (self.alarm_cfg(aid).get('name') + ': ') if len(self.alarm_list()) > 1 and self.alarm_cfg(aid).get('name') else ''
        if prev is None:
            return
        if al and not prev['al']:
            zones = [z.get('label') or ('zona %s' % z.get('id')) for z in st.get('zones', []) if z.get('alarm') or z.get('generated_alarm')]
            kind = 'POŽAR' if any(p.get('fire_alarm') for p in al) else 'PANIC' if any(p.get('panic_alarm') for p in al) else 'ALARM'
            msg = pre + '%s! %s%s' % (kind, ', '.join(p.get('label') or 'particija' for p in al), (' — ' + ', '.join(zones[:5])) if zones else '')
            self.notify('alarm', msg)
            rt = self.phone_cfg()['route']   # Telefonska linija: alarm -> SMS i poziv na zadati broj
            if rt.get('alarm_sms'):
                self.sms_reply(self.sms_num(rt['alarm_sms']), 'Teco.Pi: ' + msg)
            if rt.get('alarm_call'):
                asyncio.get_running_loop().create_task(self.ph_auto_call(rt['alarm_call']))
        for pid, on in armed.items():
            if pid in prev['armed'] and on != prev['armed'][pid]:
                lab = next((p.get('label') for p in parts if p.get('id') == pid), '') or 'alarm'
                self.notify('arm', pre + '%s je %s' % (lab, 'UKLJUČEN' if on else 'isključen'), key='arm:%s:%s:%s' % (aid, pid, on))
        if ac != prev['ac']:
            self.notify('power', 'Nestalo je struje (javlja alarm centrala).' if ac else 'Struja se vratila.', key='power:%s' % ac)

    def notify_aqua(self, lv, ph):
        prev = getattr(self, '_ntf_aqua', None) or {}
        cur = {'alarm': bool(lv.get('alarm')), 'min': lv.get('min') is False, 'ph': ph is not None and not 6.0 <= ph <= 8.5}
        self._ntf_aqua = cur
        if cur['alarm'] and not prev.get('alarm'):
            self.notify('aqua', 'Akvarijum: ALARM nivo vode (preliv)!')
        if cur['min'] and not prev.get('min'):
            self.notify('aqua', 'Akvarijum: voda je ispod MIN nivoa.')
        if cur['ph'] and not prev.get('ph'):
            self.notify('aqua', 'Akvarijum: pH je %.2f (van 6,0–8,5).' % ph, key='aqua:ph')

    async def notify_loop(self):
        """Na 60 s: praćeni uređaji iz Skeniranja mreže (3 propusta = nije na mreži) i modul akvarijuma."""
        import netscan
        sem = asyncio.Semaphore(8)
        miss = {}
        aqua_down = False
        while True:
            await asyncio.sleep(60)
            try:
                db = self.scan_db()
                watched = [(k, d) for k, d in db['devices'].items() if d.get('watch') and d.get('ip')]
                res = await asyncio.gather(*(netscan.ping(d['ip'], sem) for _, d in watched))
                for (k, d), ok in zip(watched, res):
                    name = d.get('label') or d.get('name') or d.get('vendor') or d['ip']
                    if ok:
                        if miss.get(k, 0) >= 3:
                            self.notify('devices', '%s (%s) je ponovo na mreži.' % (name, d['ip']), key='dev:%s:up' % k)
                        miss[k] = 0
                    else:
                        miss[k] = miss.get(k, 0) + 1
                        if miss[k] == 3:
                            self.notify('devices', '%s (%s) nije na mreži!' % (name, d['ip']), key='dev:%s:down' % k)
                live = getattr(self, 'aqua_live', None)
                down = bool(live) and time.time() - live.get('t', 0) > 180
                if down and not aqua_down:
                    self.notify('aqua', 'Akvarijum: modul se ne javlja već 3 min.')
                aqua_down = down
            except Exception:
                log.exception('obaveštenja')

    @staticmethod
    def _plain(s):
        for a, b in (('Š', 'S'), ('Đ', 'DJ'), ('Č', 'C'), ('Ć', 'C'), ('Ž', 'Z')):
            s = s.replace(a, b)
        return s

    async def sms_command(self, num, text):
        """SMS komanda sa pouzdanog broja; odgovor ide SMS-om."""
        c = self.notify_cfg()
        if not c['cmds'] or num not in c['nums']:
            return
        t = self._plain(text.upper()).split()
        if c['pin']:
            if not t or t[0] != c['pin']:
                return self.sms_reply(num, 'Pogrešan ili nedostaje PIN (npr. "%s STATUS").' % ('****'))
            t = t[1:]
        if not t:
            return
        log.info('SMS komanda od %s: %s', num, ' '.join(t[:3]))
        w = t[0]
        if w in ('POMOC', 'HELP', '?', 'KOMANDE'):
            return self.sms_reply(num, 'Komande: STATUS, ALARM ON, ALARM STAY, ALARM OFF, SVETLA ON/OFF, SVETLO <ime> ON/OFF, '
                                       'UREDJAJ <ime> ON/OFF, AKVARIJUM' + (' (PIN na početku)' if c['pin'] else ''))
        if w == 'STATUS':
            return self.sms_reply(num, self.status_text())
        if w == 'AKVARIJUM':
            a = self.aqua_snapshot()
            if not a['online']:
                return self.sms_reply(num, 'Akvarijum: modul nije povezan.')
            on = [o['n'] for o in a['outs'] if o['want']]
            return self.sms_reply(num, 'Akvarijum: pH %s, %s°C, uključeno: %s' % (a['ph'] if a['ph'] is not None else '?', a['temp'] if a['temp'] is not None else '?', ', '.join(on) or 'ništa'))
        if w == 'ALARM' and len(t) > 1:
            cmd = {'ON': 'arm', 'UKLJUCI': 'arm', 'STAY': 'arm_stay', 'OFF': 'disarm', 'ISKLJUCI': 'disarm'}.get(t[1])
            if not cmd:
                return self.sms_reply(num, 'ALARM ON, ALARM STAY ili ALARM OFF.')
            r = await self.alarm_do({'cmd': cmd, 'id': (self.alarm_list() or [{'id': 'a1'}])[0]['id']}, trusted=True)   # SMS sa pouzdanog broja: bez šifre stranice
            return self.sms_reply(num, r.get('ok') or r.get('err') or 'Nije uspelo.')
        if w in ('SVETLA', 'SVETLO', 'UREDJAJ', 'UREDAJ') and len(t) > 1:
            on = {'ON': True, 'UKLJUCI': True, 'OFF': False, 'ISKLJUCI': False}.get(t[-1])
            if on is None:
                return self.sms_reply(num, 'Na kraju ON ili OFF.')
            typ = 'light' if w.startswith('SVETL') else 'appliance'
            devs = [d for d in self.st.get('devices', []) if d.get('type') == typ]
            if w != 'SVETLA':
                name = ' '.join(t[1:-1])
                devs = [d for d in devs if name and name in self._plain(d.get('name', '').upper())]
                if not devs:
                    return self.sms_reply(num, 'Nema uređaja "%s".' % name.lower())
            ok = []
            for d in devs[:10]:
                r = await self.dev_cmd('dev_set', {'id': d['id'], 'on': on})
                ok.append('%s %s' % (d['name'], '✓' if not r.get('err') else '✗'))
            return self.sms_reply(num, ('Uključeno: ' if on else 'Isključeno: ') + (', '.join(ok) or 'nema uređaja'))
        return self.sms_reply(num, 'Nepoznata komanda. Pošalji POMOC.')

    def sms_reply(self, num, text):
        self.sms_add(num=num, dir='out', text=text[:600], t=time.time(), st='queued', auto='reply')
        self.sms_save()
        self.sms_kick()   # pošalji odmah (ne čekaj sledeći krug)
        self.mark()

    def status_text(self):
        out = []
        al = self.alarm or {}
        if al.get('run') == 'run':
            for p in al.get('partitions', []):
                s = 'ALARM!' if p.get('alarm') else 'uključen' if p.get('arm') else 'isključen'
                out.append('%s: %s' % (p.get('label') or 'Alarm', s))
            open_z = [z.get('label') for z in al.get('zones', []) if z.get('open')]
            if open_z:
                out.append('otvoreno: ' + ', '.join(open_z[:5]))
            tr = (al.get('system') or {}).get('troubles') or {}
            if any(v is True and k.startswith('ac_') for k, v in tr.items()):
                out.append('NEMA STRUJE')
        lights = [d['name'] for d in self.st.get('devices', []) if d.get('type') == 'light' and (getattr(self, 'dev_state', {}) or {}).get(d['id'], {}).get('on')]
        if lights:
            out.append('svetla: ' + ', '.join(lights[:5]))
        a = self.aqua_snapshot()
        if a['online']:
            out.append('akvarijum pH %s' % a['ph'])
        down = [d.get('label') or d.get('name') or d['ip'] for d in self.scan_db()['devices'].values() if d.get('watch') and not d.get('online')]
        if down:
            out.append('van mreže: ' + ', '.join(down[:5]))
        room = (getattr(self, 'sys', None) or {}).get('room')
        if isinstance(room, (int, float)):
            out.append('unutra %.1f°C' % room)
        return 'Teco.Pi ' + time.strftime('%d.%m. %H:%M') + ': ' + ('; '.join(out) or 'sve mirno')

    async def ussd(self, code):
        import modem
        code = str(code or '').strip().replace(' ', '')
        if not re.fullmatch(r'[*#][0-9*#]{1,30}#', code):
            return {'err': 'USSD kod je npr. *100#'}
        if not self.mdm_ready():
            return {'err': 'Modem nije uključen / povezan.'}
        try:
            txt = await self.mdm_call(modem.ussd, code)
        except Exception as e:
            return {'err': 'USSD: %s' % e}
        self.sms_st = getattr(self, 'sms_st', None) or {}
        self.sms_st['ussd'] = {'code': code, 'text': txt, 't': time.time()}
        self.mark()
        return {'ussd': txt}

    # ---------- Telefonska centrala (Asterisk preko ARI). Lokali: SIP (101 Damir, 102 Support…) + SLIC telefon (103).
    # Dolazni GSM poziv -> radno vreme? -> najava -> krugovi zvonjenja (samo lokali koji su online) -> govorna pošta.
    # Teco.Pi je most za zvuk: SLIC i modem su kanali u Asteriskovom mostu (externalMedia RTP <-> zvučna kartica).
    PBX_DEF = {'on': False, 'slic_ext': '103', 'vm_box': '100', 'vm_pin': '',
               'ext': {'101': {'name': 'Damir'}, '102': {'name': 'Support'}, '103': {'name': 'Telefon (SLIC)'}},
               'rounds': [{'m': ['103'], 'rings': 3}, {'m': ['101'], 'rings': 4}, {'m': ['102'], 'rings': 4}],
               'work': {'on': True, 'days': [0, 1, 2, 3, 4], 'from': '08:00', 'to': '16:00'}}

    def pbx_cfg(self):
        c = json.loads(json.dumps(self.PBX_DEF))
        s = self.st.get('pbx') or {}
        for k, v in s.items():
            if k in ('ext', 'rounds'):
                c[k] = v
            elif isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
        changed = False   # lozinke i PIN-ovi se prave jednom (nasumično) i čuvaju
        for n, e in c['ext'].items():
            if n != c['slic_ext'] and not e.get('pass'):
                e['pass'], changed = gen_pw(14), True
            if not e.get('vmpin'):
                e['vmpin'], changed = '%04d' % secrets.randbelow(10000), True
        if not c.get('vm_pin'):
            c['vm_pin'], changed = '%04d' % secrets.randbelow(10000), True
        if not c.get('ari_pass'):
            c['ari_pass'], changed = gen_pw(20, special=False), True   # ide u adresu (ARI), bez znakova
        if changed:
            self.st['pbx'] = c
            save_state(self.st)
        return c

    def pbx_on(self):
        return bool(self.pbx_cfg()['on'])

    def pbx_permit(self):
        nets = {'127.0.0.1/32', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'}
        for i in self.net.get('ifs', []):
            for a in i.get('ip') or []:
                nets.add(str(ipaddress.ip_interface(a).network))
        return sorted(nets)

    async def pbx_cli(self, cmd):
        return await self._run('sudo', '-n', '/usr/sbin/asterisk', '-rx', cmd, timeout=15)

    async def pbx_apply(self):
        """Upiši konfiguraciju (samo izmenjene fajlove) i ponovo učitaj odgovarajuće module Asteriska."""
        import pbx
        c = self.pbx_cfg()
        files = pbx.gen_config(c, c['ari_pass'], self.pbx_permit())
        reload = {'pjsip.conf': 'module reload res_pjsip.so', 'extensions.conf': 'dialplan reload', 'voicemail.conf': 'voicemail reload',
                  'ari.conf': 'module reload res_ari.so'}
        cmds = []
        for name, text in files.items():
            f = pbx.TECO_DIR / name
            try:
                if f.exists() and f.read_text() == text:
                    continue
                tmp = f.with_suffix('.tmp')
                tmp.write_text(text)
                os.chmod(tmp, 0o640)
                os.replace(tmp, f)
                cmds.append(reload[name])
            except OSError as e:
                log.warning('centrala: %s nije upisan: %s', name, e)
                return {'err': 'Centrala: podešavanje nije upisano (%s).' % e}
        rb = pbx.SOUNDS / 'ringback' / 'ringback.wav'
        if not rb.exists():
            try:
                pbx.ringback_wav(rb)
                cmds.append('moh reload')
            except OSError as e:
                log.warning('centrala: ringback: %s', e)
        for cmd in cmds:
            await self.pbx_cli(cmd)
        await self.pbx_slic_var()
        if cmds:
            log.info('centrala: primenjeno (%s)', ', '.join(cmds))
        return {}

    async def pbx_slic_var(self):
        """TECO_SLIC = 1 kad SLIC lokal može da zvoni (krugovi ga preskaču kad je isključen)."""
        on = 1 if self.phone_cfg()['slic']['on'] else 0
        if getattr(self, '_pbx_slic_var', None) != on:
            await self.pbx_cli('dialplan set global TECO_SLIC %d' % on)
            self._pbx_slic_var = on

    async def pbx_loop(self):
        """Veza sa Asteriskom (ARI) dok je centrala uključena; na 20 s: ko je online, govorna pošta."""
        import pbx
        self.pbx_ari, self.pbx_task, self.pbx_ch, self.gc = None, None, {}, None
        self._pbx_kick = asyncio.Event()
        self.pbx_st = {'eps': {}, 'vm': [], 'ok': False}
        applied = None
        while True:
            try:
                c = self.pbx_cfg()
                if c['on']:
                    key = json.dumps([c, self.phone_cfg()['slic']['on']], sort_keys=True)
                    if key != applied:
                        await self.pbx_apply()
                        applied = key
                    if not self.pbx_task or self.pbx_task.done():
                        self.pbx_ari = pbx.Ari('teco', c['ari_pass'], self.pbx_event)
                        self.pbx_task = asyncio.get_running_loop().create_task(self.pbx_ari.run())
                    st = {'ok': self.pbx_ari.ok}
                    if self.pbx_ari.ok:
                        try:
                            eps = await self.pbx_ari.req('GET', '/endpoints/PJSIP')
                            st['eps'] = {e['resource']: e.get('state') for e in eps or []}
                        except Exception:
                            st['eps'] = self.pbx_st.get('eps', {})
                    st['vm'] = await asyncio.get_running_loop().run_in_executor(None, pbx.voicemails)
                    if st != {k: self.pbx_st.get(k) for k in st}:
                        new = [m for m in st['vm'] if m['new'] and m['id'] + m['box'] not in {x['id'] + x['box'] for x in self.pbx_st.get('vm', [])}]
                        if self.pbx_st.get('vm') is not None and new and self.pbx_st.get('ok'):
                            for m in new:
                                log.info('nova govorna poruka: %s od %s', m['box'], m['from'])
                        self.pbx_st.update(st)
                        self.mark()
                elif self.pbx_task:
                    self.pbx_task.cancel()
                    self.pbx_task, self.pbx_ari = None, None
                    self.pbx_st['ok'] = False
                    self.mark()
            except Exception:
                log.exception('centrala')
            try:
                await asyncio.wait_for(self._pbx_kick.wait(), 20)
            except (asyncio.TimeoutError, AttributeError):
                if not hasattr(self, '_pbx_kick'):
                    self._pbx_kick = asyncio.Event()
            if hasattr(self, '_pbx_kick'):
                self._pbx_kick.clear()

    def pbx_kick(self):
        if getattr(self, '_pbx_kick', None):
            self._pbx_kick.set()

    # ----- ARI događaji
    def pbx_event(self, ev):
        t = ev.get('type')
        loop = asyncio.get_running_loop()
        if t in ('_connected', '_disconnected'):
            log.info('centrala: ARI %s', 'povezan' if t == '_connected' else 'prekinut')
            self.pbx_st['ok'] = t == '_connected'
            self.mark()
            return
        ch = ev.get('channel') or {}
        cid = ch.get('id')
        if t == 'StasisStart':
            if ch.get('name', '').startswith('UnicastRTP'):
                return   # naši kanali za zvuk
            args = ev.get('args') or []
            role = args[0] if args else ''
            caller = ch.get('caller') or {}
            loop.create_task(self.pbx_start(cid, role, args, caller))
        elif t in ('StasisEnd', 'ChannelDestroyed'):
            if cid in self.pbx_ch or cid == self.ph().get('pbx') or (self.gc and self.gc.get('chan') == cid):
                loop.create_task(self.pbx_gone(cid))

    async def pbx_req(self, method, path, **kw):
        try:
            return await self.pbx_ari.req(method, path, **kw)
        except Exception as e:
            log.warning('centrala: %s %s: %s', method, path, e)
            return None

    async def pbx_bridge(self, cid, device):
        """Kanal + naš zvuk (externalMedia) u isti most."""
        import pbx
        m = pbx.Media(device)
        ext = await self.pbx_req('POST', '/channels/externalMedia', app='teco', external_host='127.0.0.1:%d' % m.port, format='ulaw', direction='both')
        br = await self.pbx_req('POST', '/bridges', type='mixing')
        if not ext or not br:
            m.close()
            return False
        await self.pbx_req('POST', '/bridges/%s/addChannel' % br['id'], channel='%s,%s' % (cid, ext['id']))
        self.pbx_ch[cid] = dict(self.pbx_ch.get(cid) or {}, media=m, ext=ext['id'], bridge=br['id'])
        return True

    async def pbx_unbridge(self, cid):
        x = self.pbx_ch.pop(cid, None) or {}
        if x.get('media'):
            x['media'].close()
        if x.get('ext'):
            await self.pbx_req('DELETE', '/channels/%s' % x['ext'])
        if x.get('bridge'):
            await self.pbx_req('DELETE', '/bridges/%s' % x['bridge'])

    async def pbx_hangup(self, cid, reason='normal'):
        if cid:
            await self.pbx_req('DELETE', '/channels/%s' % cid, reason=reason)

    def modem_audio(self):
        import phoneline
        k = self.phone_cfg()['gsm'].get('audio')
        if k == 'none':
            return None
        slic_usb = self.phone_cfg()['slic']['audio'] == 'usb'
        return phoneline.alsa_device('usb', 1 if slic_usb else 0) if k == 'usb' else phoneline.alsa_device('i2s')

    async def pbx_start(self, cid, role, args, caller):
        p = self.ph()
        if role == 'slic':   # neko zove SLIC lokal
            s = self.phone_cfg()['slic']
            if not s['on'] or p['state'] != 'idle' or p['hook']:
                return await self.pbx_hangup(cid, 'busy')
            self.pbx_ch[cid] = {'role': 'slic-in'}
            num = (args[1] if len(args) > 1 else '') or caller.get('number') or ''   # Stasis(teco,slic,broj,ime)
            self.ph_set('ring_in', num=num, name=(args[2] if len(args) > 2 else '') or caller.get('name') or '', dir='in', pbx=cid, digits='')
            self.ph_ring(True)
            await self.pbx_req('POST', '/channels/%s/ring' % cid)
        elif role == 'slic-out':   # SLIC je birao, druga strana se javila
            if cid != p.get('pbx'):
                return await self.pbx_hangup(cid)
            self.ph_tone(None)
            self.pbx_ch[cid] = {'role': 'slic-out'}
            if await self.pbx_bridge(cid, self.ph_audio()):
                self.ph_set('talk', since=time.time())
        elif role == 'gsm-in':   # centrala je preuzela dolazni GSM poziv (najava / lokal se javio)
            if not self.gc or self.gc.get('chan') != cid:
                return await self.pbx_hangup(cid)
            r = await self.mdm_at('ATA', 10)
            if 'OK' not in r and 'BEGIN' not in r:
                log.warning('centrala: modem nije prihvatio poziv: %s', r.strip()[:60])
            self.pbx_ch[cid] = {'role': 'gsm-in'}
            await self.pbx_bridge(cid, self.modem_audio())
            self.gc.update(state='talk', since=time.time())
            self.mark()
        elif role == 'gsm-out':   # lokal bira spoljni broj
            num = args[1] if len(args) > 1 else ''
            if self.gc or not self.mdm_ready() or not num:
                return await self.pbx_hangup(cid, 'busy' if self.gc else 'congestion')
            self.gc = {'dir': 'out', 'num': self.sms_num(num) if not num.startswith(('*', '#')) else num, 'chan': cid, 'state': 'dial', 't': time.time(),
                       'from': caller.get('number') or ''}
            self.pbx_ch[cid] = {'role': 'gsm-out'}
            await self.pbx_req('POST', '/channels/%s/ring' % cid)
            self.mark()
            r = await self.mdm_at('ATD%s;' % num, 15)
            if 'OK' not in r and self.gc and self.gc.get('chan') == cid:
                await self.pbx_gsm_end('modem nije uspostavio poziv')
        else:
            await self.pbx_hangup(cid)

    async def pbx_gone(self, cid):
        """Kanal se završio (druga strana spustila / Asterisk prekinuo)."""
        role = (self.pbx_ch.get(cid) or {}).get('role')
        await self.pbx_unbridge(cid)
        p = self.ph()
        if cid == p.get('pbx'):
            if p['state'] == 'ring_in':
                self.ph_ring(False)
                if not self.ph_pbx_ext_only(p):
                    self.call_log('missed', p['num'])
                p['pbx'] = None
                self.ph_set('idle', num='')
            elif p['state'] in ('calling', 'talk'):
                if p['state'] == 'talk':
                    self.ph_log_end()
                p['pbx'] = None
                self.ph_set('ended', note='druga strana je spustila' if p['state'] == 'talk' else 'zauzeto / nema odgovora')
                self.ph_tone('busy')
        if self.gc and self.gc.get('chan') == cid:
            await self.mdm_at('ATH')
            self.pbx_gsm_log()
        log.debug('centrala: kanal %s (%s) završen', cid, role)

    # ----- GSM kroz centralu
    def pbx_gsm_ring(self, num):
        if num:
            num = self.sms_num(num) or num
        if self.gc:
            if self.gc['dir'] == 'in':
                self.gc['last'] = time.time()
                if num and not self.gc.get('num'):
                    self.gc['num'] = num
            return
        allow = self.phone_cfg()['route']['allow']
        if allow and num and num not in [self.sms_num(x) for x in allow]:
            log.info('poziv od %s odbijen (nije na spisku dozvoljenih)', num)
            self.call_log('rejected', num)
            asyncio.get_running_loop().create_task(self.mdm_at('ATH'))
            return
        self.gc = {'dir': 'in', 'num': num or '', 'state': 'ring', 't': time.time(), 'last': time.time()}
        self.mark()
        asyncio.get_running_loop().create_task(self.pbx_gsm_offer())

    async def pbx_gsm_offer(self):
        gc = self.gc
        if not self.pbx_ari or not self.pbx_ari.ok:
            log.warning('centrala nije povezana — dolazni poziv ide direktno na SLIC')
            self.gc = None
            return self.ph_incoming(gc['num'] or None, direct=True)
        num = gc['num'] or 'nepoznat'
        ch = await self.pbx_req('POST', '/channels', endpoint='Local/%s@teco-gsm-in' % (gc['num'] or 's'), app='teco', appArgs='gsm-in',
                                callerId='"%s" <%s>' % (num, gc['num'] or 'anonymous'), timeout=120)
        if not ch:
            self.gc = None
            return
        gc['chan'] = ch['id']
        while self.gc is gc and gc['state'] == 'ring':   # pozivalac odustao: RING prestane
            await asyncio.sleep(1)
            if time.time() - gc['last'] > 8 and self.gc is gc and gc['state'] == 'ring':
                await self.pbx_gsm_end('pozivalac odustao')

    def pbx_gsm_connected(self):
        gc = self.gc
        if gc and gc['dir'] == 'out' and gc['state'] == 'dial':
            gc.update(state='talk', since=time.time())
            self.mark()

            async def go():
                await self.pbx_req('POST', '/channels/%s/answer' % gc['chan'])
                await self.pbx_bridge(gc['chan'], self.modem_audio())
            asyncio.get_running_loop().create_task(go())

    async def pbx_gsm_end(self, why=''):
        gc = self.gc
        if not gc:
            return
        log.info('centrala: GSM poziv završen (%s)', why or 'druga strana spustila')
        self.pbx_gsm_log()
        if gc.get('chan'):
            await self.pbx_unbridge(gc['chan'])
            await self.pbx_hangup(gc['chan'])

    def pbx_gsm_log(self):
        gc, self.gc = self.gc, None
        if gc:
            if gc['state'] == 'talk':
                self.call_log(gc['dir'], gc['num'], time.time() - gc.get('since', time.time()))
            else:
                self.call_log('missed' if gc['dir'] == 'in' else 'out', gc['num'])
        self.mark()

    def pbx_view(self):
        c = self.pbx_cfg()
        st = getattr(self, 'pbx_st', None) or {}
        eps = st.get('eps') or {}
        slic_on = self.phone_cfg()['slic']['on']
        ext = [{'n': n, 'name': e['name'], 'slic': n == c['slic_ext'],
                'online': (slic_on and self.ph()['state'] != 'ring_in') if n == c['slic_ext'] else eps.get(n) == 'online'} for n, e in sorted(c['ext'].items())]
        gc = getattr(self, 'gc', None)
        heard = set(self.st.get('pbx_heard') or [])
        vm = [dict(m, heard=('%s/%s/%s' % (m['box'], m['folder'], m['id'])) in heard) for m in (st.get('vm') or [])[:100]]
        return {'on': c['on'], 'ok': bool(st.get('ok')), 'ext': ext, 'rounds': c['rounds'], 'work': c['work'], 'vm_box': c['vm_box'], 'slic_ext': c['slic_ext'],
                'prompts': {k: (Path('/var/lib/asterisk/sounds/teco') / (k + '.wav')).exists() for k in ('welcome', 'out-worktime', 'nobody')},
                'vm': vm, 'vm_new': sum(1 for m in vm if not m['heard']),
                'gsm': {k: gc.get(k) for k in ('dir', 'num', 'state', 'since')} if gc else None, 'host': self.pbx_host()}

    def pbx_host(self):
        for i in self.net.get('ifs', []):
            if i['n'] == 'wlan0' and i.get('ip'):
                return i['ip'][0].split('/')[0]
        for i in self.net.get('ifs', []):
            if i.get('ip') and i['n'] != 'tun0':
                return i['ip'][0].split('/')[0]
        return ''

    def pbx_set(self, a):
        c = self.pbx_cfg()
        if 'on' in a:
            c['on'] = bool(a['on'])
        if isinstance(a.get('names'), dict):
            for n, name in a['names'].items():
                if n in c['ext']:
                    c['ext'][n]['name'] = str(name).strip()[:30] or n
        # lokali: dodavanje / brisanje; vrsta = SIP (IP telefon / aplikacija) ili FXS (obični telefon na liniji, jedan port)
        ad = a.get('add')
        if isinstance(ad, dict):
            n = str(ad.get('n') or '').strip()
            if not re.fullmatch(r'[1-9]\d{1,3}', n):
                return {'err': 'Broj lokala: 2 do 4 cifre (npr. 104).'}
            if n in c['ext'] or n == c['vm_box']:
                return {'err': 'Lokal %s već postoji.' % n}
            if len(c['ext']) >= 20:
                return {'err': 'Najviše 20 lokala.'}
            c['ext'][n] = {'name': str(ad.get('name') or '').strip()[:30] or 'Lokal ' + n}
            if ad.get('kind') == 'fxs':
                c['slic_ext'] = n
        if a.get('del') is not None:
            n = str(a['del'])
            if n in c['ext']:
                del c['ext'][n]
                c['rounds'] = [dict(r, m=[x for x in r['m'] if x != n]) for r in c['rounds']]
                c['rounds'] = [r for r in c['rounds'] if r['m']]
                if c.get('slic_ext') == n:
                    c['slic_ext'] = None
        if isinstance(a.get('kind'), dict):   # {n: 'sip'|'fxs'}: FXS port ima samo jedan lokal
            for n, k in a['kind'].items():
                if n not in c['ext']:
                    continue
                if k == 'fxs':
                    c['slic_ext'] = n
                elif k == 'sip' and c.get('slic_ext') == n:
                    c['slic_ext'] = None
        if isinstance(a.get('rounds'), list):
            rounds = []
            for r in a['rounds'][:8]:
                m = [n for n in dict.fromkeys(str(x) for x in (r.get('m') or [])) if n in c['ext']]
                try:
                    rings = max(1, min(20, int(r.get('rings') or 3)))
                except (TypeError, ValueError):
                    rings = 3
                if m:
                    rounds.append({'m': m, 'rings': rings})
            c['rounds'] = rounds
        w = a.get('work')
        if isinstance(w, dict):
            if 'on' in w:
                c['work']['on'] = bool(w['on'])
            if isinstance(w.get('days'), list):
                c['work']['days'] = sorted({int(d) for d in w['days'] if str(d).isdigit() and 0 <= int(d) <= 6})
            for k in ('from', 'to'):
                if k in w and re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', str(w[k])):
                    c['work'][k] = str(w[k])
        self.st['pbx'] = c
        save_state(self.st)
        self.pbx_cfg()   # nov / prebačen SIP lokal odmah dobija lozinku i PIN pošte
        self.pbx_kick()
        self.mark()
        return {}

    async def pbx_cmd(self, c, a):
        import pbx
        if c == 'pbx_set':
            return self.pbx_set(a.get('cfg') or {})
        if c == 'pbx_secret':   # SIP lozinke i PIN-ovi pošte (samo Admin)
            cfg = self.pbx_cfg()
            return {'ext': {n: {'pass': e.get('pass', ''), 'vmpin': e.get('vmpin', '')} for n, e in cfg['ext'].items()}, 'vm_pin': cfg['vm_pin'], 'host': self.pbx_host()}
        if c == 'pbx_prompt_del':
            name = str(a.get('name') or '')
            if name in pbx.PROMPTS:
                try:
                    (pbx.SOUNDS / (name + '.wav')).unlink()
                except OSError:
                    pass
                self.mark()
            return {}
        if c in ('pbx_vm_del', 'pbx_vm_heard'):
            box, folder, mid = str(a.get('box') or ''), str(a.get('folder') or ''), str(a.get('id') or '')
            if not (re.fullmatch(r'\d{2,6}', box) and folder in ('INBOX', 'Old', 'Urgent') and re.fullmatch(r'msg\d{4}', mid)):
                return {'err': 'Nepoznata poruka.'}
            key = '%s/%s/%s' % (box, folder, mid)
            if c == 'pbx_vm_heard':
                heard = set(self.st.get('pbx_heard') or [])
                heard.add(key)
                self.st['pbx_heard'] = sorted(heard)[-500:]
            else:
                for ext in ('.wav', '.txt', '.WAV', '.gsm'):
                    try:
                        (pbx.VM_DIR / box / folder / (mid + ext)).unlink()
                    except OSError:
                        pass
            save_state(self.st)
            self.pbx_kick()
            self.mark()
            return {}
        return {'err': 'Nepoznata komanda.'}

    def tool_set(self, name, a):
        """Podešavanje alata sa stranice (samo poznata polja, u granicama)."""
        cur = self.tool_cfg(name)
        num = lambda v, lo, hi, d: max(lo, min(hi, float(v))) if v not in (None, '') else d
        try:
            if name == 'scope':
                if a.get('exp') in ('exp1', 'exp2'):   # proširenje na kom je kartica osciloskopa / generatora
                    if a['exp'] == self.phone_cfg()['exp']:
                        return {'err': 'Na %s je već Telefonska linija.' % a['exp'].upper()}
                    cur['exp'] = a['exp']
                if 'run' in a:
                    cur['run'] = bool(a['run'])
                if 'tdiv' in a:
                    cur['tdiv'] = num(a['tdiv'], 1e-6, 1.0, cur['tdiv'])
                tr = a.get('trig') or {}
                if tr.get('src') in ('ch1', 'ch2'):
                    cur['trig']['src'] = tr['src']
                if tr.get('edge') in ('rise', 'fall'):
                    cur['trig']['edge'] = tr['edge']
                if 'level' in tr:
                    cur['trig']['level'] = num(tr['level'], -50, 50, 0.0)
                for ch in ('ch1', 'ch2'):
                    c = a.get(ch) or {}
                    if 'on' in c:
                        cur[ch]['on'] = bool(c['on'])
                    if c.get('wave') in ('sine', 'square', 'triangle', 'saw', 'dc', 'noise'):
                        cur[ch]['wave'] = c['wave']
                    for k, lo, hi in (('freq', 0.01, 1e6), ('amp', 0, 50), ('offset', -50, 50), ('vdiv', 0.001, 50), ('noise', 0, 1)):
                        if k in c:
                            cur[ch][k] = num(c[k], lo, hi, cur[ch][k])
            elif name == 'logic':
                if a.get('srcmode') in ('sim', 'pk2'):
                    cur['srcmode'] = a['srcmode']
                p = a.get('pk2') or {}
                import pickit2
                if p.get('rate') in pickit2.RATES:
                    cur['pk2']['rate'] = p['rate']
                if p.get('win') in pickit2.WINDOWS:
                    cur['pk2']['win'] = p['win']
                if p.get('trig') in ('rise', 'fall', 'high', 'low'):
                    cur['pk2']['trig'] = p['trig']
                if str(p.get('trig_ch')) in ('0', '1', '2', '3'):
                    cur['pk2']['trig_ch'] = int(p['trig_ch'])
                if str(p.get('uart')) in ('0', '1', '2', '3'):
                    cur['pk2']['uart'] = int(p['uart'])
                if p.get('baud') not in (None, ''):
                    cur['pk2']['baud'] = max(50, min(500000, int(p['baud'])))
                if 'run' in a:
                    cur['run'] = bool(a['run'])
                if 'tdiv' in a:
                    cur['tdiv'] = num(a['tdiv'], 1e-7, 1.0, cur['tdiv'])
                tr = a.get('trig') or {}
                if tr.get('src') in ('ch0', 'ch1'):
                    cur['trig']['src'] = tr['src']
                if tr.get('edge') in ('rise', 'fall'):
                    cur['trig']['edge'] = tr['edge']
                for ch in ('ch0', 'ch1'):
                    c = a.get(ch) or {}
                    if 'on' in c:
                        cur[ch]['on'] = bool(c['on'])
                    if c.get('src') in ('clock', 'pwm', 'uart', 'pattern'):
                        cur[ch]['src'] = c['src']
                    for k, lo, hi in (('freq', 0.1, 1e7), ('duty', 1, 99), ('rate', 1, 1e7)):
                        if k in c:
                            cur[ch][k] = num(c[k], lo, hi, cur[ch][k])
                    if c.get('baud') in (300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200) or str(c.get('baud')) in ('300', '1200', '2400', '4800', '9600', '19200', '38400', '57600', '115200'):
                        cur[ch]['baud'] = int(c['baud'])
                    if 'text' in c:
                        cur[ch]['text'] = str(c['text'])[:40]
                    if 'bits' in c:
                        cur[ch]['bits'] = re.sub(r'[^01]', '', str(c['bits']))[:64] or '10'
            elif name == 'resistor':
                if a.get('mode') in ('bands', 'smd'):
                    cur['mode'] = a['mode']
                if a.get('bands') in (3, 4, 5, 6, '3', '4', '5', '6'):
                    cur['bands'] = int(a['bands'])
                if isinstance(a.get('colors'), list):
                    cur['colors'] = [str(x)[:10] for x in a['colors'][:6]]
                if 'smd' in a:
                    cur['smd'] = re.sub(r'[^0-9A-Za-zR.]', '', str(a['smd']))[:5].upper()
            elif name == 'guest':   # Wi-Fi za goste (QR na ekranu)
                if 'ssid' in a:
                    cur['ssid'] = str(a['ssid'])[:32]
                if 'pw' in a:
                    cur['pw'] = str(a['pw'])[:63]
                if a.get('sec') in ('WPA', 'WEP', 'nopass'):
                    cur['sec'] = a['sec']
                if 'hidden' in a:
                    cur['hidden'] = bool(a['hidden'])
            elif name == 'timer':   # tajmer: odbrojavanje (set/start/pause/reset/add), štoperica (sw_*), alarm (al_*), stop zvona
                now, act = time.time(), a.get('act')
                if 'label' in a:
                    cur['label'] = str(a['label'])[:30]
                if a.get('mode') in ('cd', 'sw', 'al'):
                    cur['mode'] = a['mode']
                if act == 'stop':   # ugasi zvono (alarm / isteklo odbrojavanje)
                    cur['ring'] = 0
                    self.timer_sound(False)
                    if cur['run'] and cur['end'] <= now:
                        cur.update(run=False, left=cur['dur'], end=0)
                elif act == 'sw_start' and not cur['sw_run']:
                    cur.update(sw_run=True, sw_start=now)
                elif act == 'sw_pause' and cur['sw_run']:
                    cur.update(sw_run=False, sw_acc=round(cur['sw_acc'] + now - cur['sw_start'], 2))
                elif act == 'sw_reset':
                    cur.update(sw_run=False, sw_acc=0, sw_start=0, laps=[])
                elif act == 'sw_lap':
                    el = cur['sw_acc'] + (now - cur['sw_start'] if cur['sw_run'] else 0)
                    cur['laps'] = (cur['laps'] + [round(el, 2)])[-30:]
                elif act == 'al_set':
                    at = str(a.get('at') or '')
                    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', at):
                        return {'err': 'Vreme alarma: SS:MM (npr. 07:30).'}
                    cur.update(al_at=at, al_on=True, al_last='')
                elif act == 'al_off':
                    cur.update(al_on=False)
                if act == 'set':
                    cur['dur'] = cur['left'] = max(1, min(86400, int(a.get('dur') or 0)))
                    cur.update(run=False, end=0)
                elif act == 'start' and not cur['run']:
                    if cur['left'] <= 0:
                        cur['left'] = cur['dur']
                    cur.update(run=True, end=now + cur['left'])
                elif act == 'pause' and cur['run']:
                    cur.update(run=False, left=max(0, round(cur['end'] - now, 1)), end=0)
                elif act == 'reset':
                    cur.update(run=False, left=cur['dur'], end=0)
                elif act == 'add':
                    sec = int(a.get('sec') or 60)
                    if cur['run']:
                        cur['end'] = max(now, cur['end']) + sec
                    else:
                        cur['left'] = max(0, cur['left']) + sec
            elif name == 'prog':   # programatori na ekranu: koji je otvoren (esp / avr / pic)
                if a.get('mode') in ('esp', 'avr', 'pic'):
                    self.prog_tvmode = a['mode']
            elif name == 'dice':   # kockice: broj, vrsta i poslednje bacanje
                if int(a.get('sides', cur['sides'])) in (4, 6, 8, 10, 12, 20, 100):
                    cur['sides'] = int(a.get('sides', cur['sides']))
                cur['n'] = max(1, min(10, int(a.get('n', cur['n']))))
                if 'vals' in a:
                    cur['vals'] = [max(1, min(cur['sides'], int(x))) for x in list(a['vals'])[:10]]
                    cur['t'] = time.time()
            elif name == 'ser':   # serijski terminal na ekranu: vreme i HEX kao na stranici
                for k in ('ts', 'hex'):
                    if k in a:
                        cur[k] = bool(a[k])
            elif name == 'pin':   # Pinout na ekranu: ceo čip (JSON tekst, da se stari ključevi ne mešaju) + izabrani pin
                if 'chipj' in a:
                    s = str(a['chipj'])
                    if len(s) > 40000:
                        raise ValueError
                    ch = json.loads(s)
                    if not isinstance(ch, dict):
                        raise ValueError
                    cur['chip'] = ch
                if 'sel' in a:
                    cur['sel'] = int(a['sel'])
            elif name == 'tran':
                if 'q' in a:
                    cur['q'] = re.sub(r'[^0-9A-Za-z\- ]', '', str(a['q']))[:16]
            elif name == 'ind':
                import components
                if a.get('mode') in ('smd', 'bands'):
                    cur['mode'] = a['mode']
                if 'smd' in a:
                    cur['smd'] = re.sub(r'[^0-9A-Za-zµ., ]', '', str(a['smd']))[:10]
                if isinstance(a.get('bands'), list):
                    cur['bands'] = [b if b in components.COL_RGB else 'none' for b in a['bands'][:4]]
            elif name == 'capacitor':
                if a.get('mode') in ('code', 'smd'):
                    cur['mode'] = a['mode']
                for k in ('code', 'smd'):   # mala/velika slova su bitna (EIA-198: a, b, d…; p n u)
                    if k in a:
                        cur[k] = re.sub(r'[^0-9A-Za-zµ. ]', '', str(a[k]))[:14]
        except (TypeError, ValueError):
            return {'err': 'Neispravna vrednost.'}
        self.st.setdefault('tools', {})[name] = cur
        save_state(self.st)
        self.tool_write(name)
        self.mark()
        return {}

    # ---------- Akvarijum (Smart Home): izlaze i senzore drži ESP32 („Teco ESP“ protokol, /api/aqua).
    # Teco pamti ŽELJENO stanje izlaza (sa stranice); ESP32 na 1-2 s javi pH, nivoe i STVARNO stanje izlaza,
    # a u odgovoru dobije željeno. Bez javljanja > 10 s: ESP nije na vezi.
    AQUA_OUTS = [('pump1', 'Pumpa 1'), ('air', 'Pumpa za kiseonik'), ('heater1', 'Grejač 1'), ('light1', 'Svetlo 1'), ('light2', 'Svetlo 2 · RGB'),
                 ('fan', 'Ventilator'), ('valve_in', 'Ventil dovoda vode'), ('pump_out', 'Pumpa odvoda vode')]
    AQUA_RGB = ('light2',)   # RGB traka: uz uključeno/isključeno i boja #RRGGBB (ESP dobije "rgb": {"light2": [r, g, b]})
    AQUA_LEVELS = [('min', 'Min'), ('q34', '3/4'), ('max', 'Max'), ('alarm', 'Alarm')]

    def aqua_cfg(self):
        a = self.st.setdefault('aqua', {})
        w = a.setdefault('want', {})
        for k, _ in self.AQUA_OUTS:   # novi izlazi (npr. ventilator) kreću isključeni
            w.setdefault(k, False)
        rgb = a.setdefault('rgb', {})
        for k in self.AQUA_RGB:
            rgb.setdefault(k, '#3b82f6')
        return a

    def aqua_rgb_list(self):
        return {k: [int(c[i:i + 2], 16) for i in (1, 3, 5)] for k, c in self.aqua_cfg()['rgb'].items()}

    def aqua_snapshot(self):
        a, live = self.aqua_cfg(), getattr(self, 'aqua_live', None) or {}
        online = bool(live) and time.time() - live.get('t', 0) < 10
        return {'outs': [{'k': k, 'n': n, 'want': bool(a['want'].get(k)), 'on': live.get('outs', {}).get(k) if online else None,
                          **({'rgb': a['rgb'].get(k)} if k in self.AQUA_RGB else {})} for k, n in self.AQUA_OUTS],
                'levels': [{'k': k, 'n': n, 'wet': live.get('levels', {}).get(k) if online else None} for k, n in self.AQUA_LEVELS],
                'ph': live.get('ph') if online else None, 'temp': live.get('temp') if online else None,
                'online': online, 'seen': live.get('t'), 'esp': live.get('id')}

    def aqua_report(self, q):
        """ESP32 javlja stanje; vraća željene izlaze. Zaštita: dovod vode se gasi na MAX/ALARM nivou,
        odvod se gasi kad voda padne ispod MIN (ESP treba to da radi i sam, ovo je druga linija zaštite)."""
        a = self.aqua_cfg()
        lv = {k: (bool(q['levels'][k]) if isinstance(q.get('levels'), dict) and k in q['levels'] else None) for k, _ in self.AQUA_LEVELS}
        outs = {k: bool(v) for k, v in (q.get('outs') or {}).items() if k in a['want']}
        ph = q.get('ph')
        try:
            ph = round(float(ph), 2) if ph not in (None, '') else None
            if ph is not None and not 0 <= ph <= 14:
                ph = None
        except (TypeError, ValueError):
            ph = None
        try:
            temp = round(float(q['temp']), 1) if q.get('temp') not in (None, '') else None
        except (TypeError, ValueError):
            temp = None
        changed = False
        if (lv.get('max') or lv.get('alarm')) and a['want'].get('valve_in'):
            a['want']['valve_in'] = False
            changed = True
            log.info('akvarijum: nivo MAX/ALARM — ventil dovoda zatvoren')
        if lv.get('min') is False and a['want'].get('pump_out'):
            a['want']['pump_out'] = False
            changed = True
            log.info('akvarijum: voda ispod MIN — pumpa odvoda ugašena')
        if changed:
            save_state(self.st)
        prev = getattr(self, 'aqua_live', None) or {}
        if not prev or time.time() - prev.get('t', 0) >= 10:
            log.info('akvarijum: ESP %s na vezi', q.get('id') or '')
        self.aqua_live = {'t': time.time(), 'id': str(q.get('id') or 'esp32')[:30], 'outs': outs, 'levels': lv, 'ph': ph, 'temp': temp}
        self.notify_aqua(lv, ph)
        self.mark()
        return {'ok': True, 'outs': dict(a['want']), 'rgb': self.aqua_rgb_list()}

    SRC_ROLES = ('av2', 'av3', 'av4', 'tuner')   # releji ulaza: uvek najviše jedan uključen

    def _relay_hw(self, i, on):
        self.st['relays'][i]['on'] = bool(on)
        for dev in self.gpio[i] if i < len(self.gpio) else []:
            dev.value = bool(on)

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
            if getattr(self, 'mpv_back', False):   # mpv vraćen posle igre: novi izvor sam postavlja sliku
                self.mpv_back = False
            elif self.source == 'arc' and not self.arc_running():   # restart servera/plejera na Arcade
                asyncio.get_running_loop().create_task(self.show_arcade())
            else:   # posle restarta servera izvor je AV1: skloni sliku konzole ako je ostala na ekranu
                asyncio.get_running_loop().create_task(self.hide_console())
        if name == 'idle-active' and self.mpv.props.get('idle-active') and self.mode == 'yt':
            asyncio.get_running_loop().create_task(self.start_slides())
        elif name == 'idle-active' and self.mpv.props.get('idle-active') and self.tool_running():
            asyncio.get_running_loop().create_task(self.tool_base())   # plejer se ispraznio (restart, kraj videa) dok je alat na ekranu
        if name == 'path' and self.is_black() and not self.tool_running():   # ostala crna podloga bez alata (npr. posle restarta servera)
            asyncio.get_running_loop().create_task(self.tool_base_off())
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
        if name == 'time-pos':   # pozicija videa se menja ~10×/s: stranicama jednom u sekundi (ceo snapshot je ~50 kB), skok odmah
            pos, now = self.mpv.props.get('time-pos') or 0, time.monotonic()
            lp, lt = getattr(self, 'pos_sent', (0, 0))
            if now - lt < 1 and abs(pos - lp - (now - lt)) < 1.5:
                return
            self.pos_sent = (pos, now)
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
    def _extract_fast(url):
        """Brza priprema (~2-4 s umesto 6-7 s): YouTube „tv_embedded“ klijent daje isti 480p H.264 + zvuk,
        bez rešavanja JS zadatka u Node-u. Ne radi za videe koji ne dozvoljavaju ugrađivanje i za one sa
        ograničenjem uzrasta — tada se koristi obična priprema (_extract, sa kolačićima)."""
        import yt_dlp
        opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True, 'format': YT_FMT,
                'extractor_args': {'youtube': {'player_client': ['tv_embedded'], 'skip': ['hls']}}}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
        if info.get('entries'):
            info = info['entries'][0]
        rf = info.get('requested_formats')
        return info.get('title') or url, float(info.get('duration') or 0), [f['url'] for f in rf] if rf else [info['url']]
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

    @staticmethod
    def _url_ok(u):
        """Da li YouTube link još prima zahtev kao plejer (Range od početka do kraja); čita se samo 1 bajt."""
        import urllib.request
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={'Range': 'bytes=0-'}), timeout=4) as r:
                r.read(1)
                return r.status in (200, 206)
        except Exception:
            return False

    def _rc_put(self, key, title, dur, urls):
        m = re.search(r'[?&]expire=(\d+)', urls[0])
        self.rcache[key] = {'t': title, 'd': dur, 'urls': urls, 'exp': int(m.group(1)) if m else time.time() + 3600}
        for k in [k for k, v in self.rcache.items() if v['exp'] < time.time()]:
            del self.rcache[k]
        try:
            tmp = DATA / 'rcache.tmp'
            tmp.write_text(json.dumps(self.rcache))
            os.replace(tmp, DATA / 'rcache.json')
        except OSError:
            pass

    async def resolve(self, url, fast=False):
        """Pripremljeni linkovi važe ~6 h (parametar expire), pa se pamte (i na disku): ponovno puštanje kreće odmah.
        Prvo brza priprema (tv_embedded), a ako YouTube to ne dozvoli za ovaj video — obična (fast se ne koristi više)."""
        key = self._vid(url)
        hit = self.rcache.get(key)
        loop = asyncio.get_running_loop()
        if hit and hit['exp'] - time.time() > 900:
            # YouTube posle prvog gledanja ume da „zaključa“ link (plejer tada dobije 403 na Range: bytes=0-),
            # pa se keširani link kratko proveri; ako ga više ne prima — nova priprema
            if await loop.run_in_executor(None, self._url_ok, hit['urls'][0]):
                return hit['t'], hit['d'], hit['urls']
            log.info('YouTube %s: link iz keša više ne važi, pripremam ponovo', key)
            self.rcache.pop(key, None)
        t0 = time.monotonic()
        how = 'brzo'
        try:
            if not self.ytdlp_ok:
                raise RuntimeError('yt_dlp nije učitan')
            title, dur, urls = await loop.run_in_executor(self.ytpool, self._extract_fast, url)
        except Exception as e:
            how = 'obično'
            log.info('YouTube %s: brza priprema ne ide (%s)', key, str(e).replace('ERROR: ', '')[:100])
            if self.ytdlp_ok:
                title, dur, urls = await loop.run_in_executor(self.ytpool, self._extract, url)
            else:
                title, dur, urls = await self._resolve_cli(url)
        log.info('YouTube %s pripremljen za %.1f s (%s)', key, time.monotonic() - t0, how)
        self._rc_put(key, title, dur, urls)
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
            self.jsc_worker = worker   # Arcade ga gasi dok se igra (sam se ponovo pali za sledeći video)

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
            title, dur, urls = await self.resolve(url, fast=now)
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
            if i < cur or e.get('filename') == str(self.TOOL_BLACK):
                continue  # već pušteni su u istoriji; crna podloga alata nije video
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
        vw = screen_vw(w, h)
        key = hashlib.md5(json.dumps(parts + [w, h, 7], ensure_ascii=False).encode()).hexdigest()[:12]
        if keep:
            (DATA / 'ovl').mkdir(exist_ok=True)
        raw = (DATA / 'ovl' / ('%s.bgra' % key)) if keep else OVL_TMP / ('teco-ovl-%s.bgra' % key)
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
    def _overlay_prune(keep, n=6):
        """Slike za ekran su u RAM-u, a jedna na 1080p ima 8 MB: čuvaj samo poslednjih n."""
        try:
            os.utime(keep)
            files = sorted(OVL_TMP.glob('teco-ovl-*.bgra'), key=lambda p: p.stat().st_mtime, reverse=True)
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

    @staticmethod
    def arc_svg(w, h, title, line):
        """Arcade slika: PlayStation 1 konzola, ARCADE, naslov i red teksta (za mpv overlay i framebuffer)."""
        try:
            c = json.loads((STATIC / 'consoles.json').read_text())['ps1']
        except (OSError, ValueError, KeyError):
            c = {'svg': '<svg viewBox="0 0 260 150"></svg>'}
        color = '#f59e0b'
        ch = int(h * 0.46)
        cw = int(ch * 260 / 150)
        if cw > w * 0.8:
            cw = int(w * 0.8)
            ch = int(cw * 150 / 260)
        cx, cy = (w - cw) // 2, int(h * 0.1)
        inner = c['svg'].replace('<svg viewBox="0 0 260 150"', '<svg x="%d" y="%d" width="%d" height="%d" viewBox="0 0 260 150"' % (cx, cy, cw, ch), 1)
        ty = cy + ch + int(h * 0.1)
        esc = Teco._esc
        return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<defs><radialGradient id="bgGlow" cx="50%" cy="0%" r="90%"><stop offset="0" stop-color="{color}" stop-opacity=".35"/><stop offset="1" stop-color="{color}" stop-opacity="0"/></radialGradient></defs>
<rect width="{w}" height="{h}" fill="#0b1018"/><rect width="{w}" height="{h}" fill="url(#bgGlow)"/>
<ellipse cx="{w // 2}" cy="{cy + ch * 0.78:.0f}" rx="{cw * 0.4:.0f}" ry="{h * 0.02:.0f}" fill="#000" fill-opacity=".5"/>
{inner}
<text x="{w // 2}" y="{ty}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.038:.0f}" letter-spacing="3" fill="{color}">ARCADE</text>
<text x="{w // 2}" y="{ty + h * 0.095:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-weight="bold" font-size="{h * 0.075:.0f}" fill="#ffffff">{esc(title[:34])}</text>
<text x="{w // 2}" y="{ty + h * 0.165:.0f}" text-anchor="middle" font-family="DejaVu Sans" font-size="{h * 0.036:.0f}" fill="#9aa4b3">{esc(line)}</text>
</svg>'''

    def arc_line(self):
        n = len(self.arc_games())
        return ('%d %s · izaberi igru na telefonu' % (n, 'igra' if n % 10 == 1 and n % 100 != 11 else 'igre' if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 'igara')) \
            if n else 'Nema igara — stavi ih na flash u folder CD'

    async def show_arcade(self, show=True):
        """Arcade bez pokrenute igre: PlayStation 1 slika (kao konzole na AV ulazima)."""
        line = self.arc_line()
        await self._overlay(['arc', line], lambda w, h: self.arc_svg(w, h, 'PlayStation 1', line), keep=True, show=show)

    async def arc_fb(self, title, line):
        """Arcade slika u framebuffer (/dev/fb0): vidi se u prelazu plejer <-> emulator umesto Teco.Pi logoa.
        Kešira se (data/ovl/arcfb-*.fb); posle igre arc_fb_reset vraća Teco.Pi logo."""
        try:
            w, h = (int(x) for x in Path('/sys/class/graphics/fb0/virtual_size').read_text().split(','))
            f = DATA / 'ovl' / ('arcfb-%s.fb' % hashlib.md5(json.dumps([title, line, w, h, 1], ensure_ascii=False).encode()).hexdigest()[:12])
            if not f.exists():
                f.parent.mkdir(exist_ok=True)
                vw = screen_vw(w, h)
                svg = f.with_suffix('.svg')
                svg.write_text(self.arc_svg(vw, h, title, line))
                png = f.with_suffix('.png')
                p = await asyncio.create_subprocess_exec('rsvg-convert', '-w', str(vw), '-h', str(h), '-o', str(png), str(svg))
                await p.wait()

                def conv():
                    from PIL import Image, ImageChops
                    r, g, b = Image.open(png).convert('RGB').resize((w, h), Image.LANCZOS).split()
                    # RGB565 little-endian (vc4drmfb, 16 bita): nizi bajt GGGBBBBB, viši RRRRRGGG — bitovi se ne preklapaju, pa je zbir tačan
                    lo = ImageChops.add(g.point(lambda v: ((v >> 2) & 7) << 5), b.point(lambda v: v >> 3))
                    hi = ImageChops.add(r.point(lambda v: (v >> 3) << 3), g.point(lambda v: v >> 5))
                    f.write_bytes(Image.merge('LA', (lo, hi)).tobytes())
                await asyncio.get_running_loop().run_in_executor(None, conv)
                svg.unlink(missing_ok=True)
                png.unlink(missing_ok=True)
            await asyncio.get_running_loop().run_in_executor(None, lambda: Path('/dev/fb0').write_bytes(f.read_bytes()))
        except Exception as e:
            log.warning('Arcade slika (fb0): %s', e)

    async def arc_fb_reset(self):
        try:
            await asyncio.get_running_loop().run_in_executor(None, lambda: Path('/dev/fb0').write_bytes((DATA / 'splash.fb').read_bytes()))
        except OSError:
            pass
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
        if self.tool_running():   # izabran je izvor: alat se sklanja sa ekrana
            await self.tool_hide(restore=False)
        if prev == 'arc' and s != 'arc':   # prvo ugasi igru i sačekaj mpv — tek onda slika novog izvora
            await self.arc_stop()
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
        if s == 'arc' and prev != 'arc' and self.mode == 'yt':   # YouTube lista se pamti dok se igra (mpv se gasi za emulator)
            self.yt_snap = self.yt_snapshot() or self.yt_snap
        self.st['last_source'] = s
        if s in AVS:
            self.st['last_av'] = s   # AV dugme na stranici pamti poslednji ulaz
        for k in AVS:   # AV releji: uključen samo ulaz koji je izabran
            self.relay_role(k, s == k)
        save_state(self.st)
        self.mark()   # stranica odmah vidi novi izvor (slika za ekran se ume crtati i par sekundi)
        asyncio.get_running_loop().create_task(self.scr_input('AV' if s in AVS else 'HDMI'))   # ulaz na ekranu (dugme SOURCE)
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
        elif s == 'arc':
            if not self.arc_running():
                await self.show_arcade()
        elif s == 'av1' and not getattr(self, 'yt_loading', None):
            await self.hide_console()
        if s != 'av1':
            self.yt_loading = None
        self.mark()

    # ---------- Arcade: PS1 emulator (RetroArch + PCSX ReARMed), radi SAMO dok se igra
    def arc_games(self):
        """Igre sa USB diskova (keš). Pretraga flasha ide u pozadinskoj niti na 30 s — inače blokira server
        (spor USB); stranica do tada vidi poslednju listu. Samo prvi put (prazan keš) traži odmah."""
        c = getattr(self, '_arc_cache', None)
        if c is None:
            return self._arc_scan()
        if time.time() - c[0] >= 30 and not getattr(self, '_arc_scanning', False):
            self._arc_scanning = True
            loop = asyncio.get_running_loop()

            def done(f):
                self._arc_scanning = False
                if not f.exception() and [g['id'] for g in f.result()] != [g['id'] for g in c[1]]:
                    self.mark()   # lista igara se promenila (flash izvađen/ubačen)
            loop.run_in_executor(None, self._arc_scan).add_done_callback(done)
        return c[1]

    def _arc_scan(self):
        """<disk>/CD/<igra>/*.cue|chd|pbp|iso (ili fajl direktno u CD); i da li postoji BIOS."""
        games = []
        for cd in sorted(glob.glob(str(USB_MNT / '*' / 'CD'))):
            for e in sorted(os.scandir(cd), key=lambda x: x.name.lower()):
                if e.name.startswith(('.', '__MACOSX')):
                    continue
                files = [e.path] if e.is_file() else sorted(
                    f for pat in ('*', '*/*') for f in glob.glob(os.path.join(glob.escape(e.path), pat))   # i jedan podfolder dublje
                    if '__MACOSX' not in f and not os.path.basename(f).startswith('.')) if e.is_dir() else []
                pick = next((f for ext in ARC_EXT for f in files if f.lower().endswith(ext)), None)
                if pick:
                    name = e.name if e.is_dir() else os.path.splitext(e.name)[0]
                    reg = next((r for r in ('USA', 'Europe', 'Japan', 'PAL', 'NTSC') for t in (name, os.path.basename(pick)) if '(%s' % r in t), '')
                    # omot: game-art.jpg/png u folderu igre (ili cover/folder.*)
                    art = next((f for stem in ('game-art', 'cover', 'folder') for f in files
                                if os.path.splitext(os.path.basename(f))[0].lower() == stem
                                and f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))), None)
                    games.append({'id': hashlib.md5(pick.encode()).hexdigest()[:10], 'name': re.sub(r'\s*\([^)]*\)', '', name).strip() or name,
                                  'region': reg, 'path': pick, 'sys': 'PS1', 'art': art,
                                  'artv': int(os.stat(art).st_mtime) if art else 0})
        self._arc_bios = any((ARC_DIR / 'bios').glob('scph*')) if ARC_DIR.exists() else False
        self._arc_cache = (time.time(), games)
        return games

    def arc_cfg(self):
        """Minimalni RetroArch: slika direktno na ekran (KMS), zvuk PipeWire, tastatura/džojstik (udev).
        Na kompozitnom (PAL) TV-u: 288p 50 Hz + igra u PAL ritmu (50 fps) -> vsync i zvuk usklađeni."""
        for sub in ('saves', 'states', 'bios'):
            (ARC_DIR / sub).mkdir(parents=True, exist_ok=True)
        opts = {
            'video_driver': 'gl', 'video_context_driver': 'kms', 'video_fullscreen': 'true', 'video_vsync': 'false',
            'video_threaded': 'false', 'video_smooth': 'false', 'video_refresh_rate': '50.000000', 'video_max_swapchain_images': '2',
            'audio_driver': 'pulse', 'audio_latency': '128', 'audio_sync': 'true', 'audio_rate_control': 'true', 'audio_rate_control_delta': '0.005',
            'input_driver': 'udev', 'input_joypad_driver': 'udev', 'input_autodetect_enable': 'true', 'menu_driver': 'rgui', 'video_font_enable': 'false',
            'pause_nonactive': 'false', 'config_save_on_exit': 'false', 'menu_show_load_content_animation': 'false',
            'savefile_directory': str(ARC_DIR / 'saves'), 'savestate_directory': str(ARC_DIR / 'states'), 'system_directory': str(ARC_DIR / 'bios'),
            'libretro_directory': str(ARC_CORES), 'core_options_path': str(ARC_DIR / 'core-options.cfg'),
            'input_exit_emulator': 'escape', 'input_menu_toggle': 'f1', 'input_save_state': 'f2', 'input_load_state': 'f4',
            'input_player1_start': 'enter', 'input_player1_select': 'rshift', 'input_player1_a': 'x', 'input_player1_b': 'z',
            'input_player1_x': 's', 'input_player1_y': 'a', 'input_player1_l': 'q', 'input_player1_r': 'w',
            'input_player1_l2': 'num1', 'input_player1_r2': 'num2',
        }
        # podrazumevane RetroArch prečice koje se lako slučajno pritisnu (Space = ubrzanje, kvari zvuk; L, K, E, F, P, O, H...)
        for k in ('toggle_fast_forward', 'hold_fast_forward', 'frame_advance', 'slowmotion', 'hold_slowmotion', 'toggle_fullscreen',
                  'pause_toggle', 'reset', 'rewind', 'movie_record_toggle', 'shader_next', 'shader_prev', 'audio_mute', 'osk_toggle',
                  'screenshot', 'netplay_game_watch', 'game_focus_toggle', 'cheat_index_plus', 'cheat_index_minus', 'cheat_toggle',
                  'volume_up', 'volume_down', 'state_slot_increase', 'state_slot_decrease', 'disk_eject_toggle', 'disk_next', 'disk_prev',
                  'fps_toggle', 'recording_toggle', 'streaming_toggle', 'ai_service', 'runahead_toggle', 'close_content', 'grab_mouse_toggle'):
            opts['input_' + k] = 'nul'
        try:
            hdmi = Path('/sys/class/drm/card0-HDMI-A-1/enabled').read_text().strip() == 'enabled'   # izlaz stvarno radi (ne samo kabl)
        except OSError:
            hdmi = False
        # format slike (Arcade → Slika): '43' originalni PS1 4:3, 'wide' preko celog (širokog) ekrana
        wide = self.st.get('arc_aspect', 'wide') == 'wide'
        if not hdmi:   # kompozitni (AV) izlaz: ceo ekran 720x576 je 4:3 (aspect_ratio_index 20 = vrednost iz video_aspect_ratio)
            # 576i (standardni PAL): 288p „pravi konzola“ signal mnogi LCD ekrani sa AV ulazom ne hvataju (slika igra);
            # brzo jezgro (NEON) u 576i stiže punih 50 fps (provereno 14.10.2026)
            opts.update({'video_fullscreen_x': '720', 'video_fullscreen_y': '576', 'video_refresh_rate': '50.000000',
                         # 720x576 ekran razvlači na svoj (širok) oblik: 1.25 = preko celog; 4:3 = 1.25 * (4/3) / SCREEN_ASPECT
                         'aspect_ratio_index': '20', 'video_aspect_ratio': '1.25' if wide else '%.4f' % (1.25 * 4 / 3 / SCREEN_ASPECT), 'video_vsync': 'true',
                         # Bluetooth uzima zvuk u blokovima ~43 ms: veći bafer da se ni slika ni zvuk ne trzaju
                         'audio_latency': '256'})
        else:   # HDMI (npr. 1024x600 @ 60 Hz): kvadratni pikseli; 4:3 sa trakama sa strane ili preko celog ekrana; NTSC punih 60 fps
            try:
                fw, fh = (int(x) for x in Path('/sys/class/graphics/fb0/virtual_size').read_text().split(','))
            except (OSError, ValueError):
                fw, fh = 16, 9
            opts.update({'video_refresh_rate': '60.000000', 'aspect_ratio_index': '20', 'video_aspect_ratio': '%.4f' % (fw / fh) if wide else '1.333333',
                         'video_vsync': 'true', 'audio_latency': '256'})
        opts['global_core_options'] = 'true'   # inače RetroArch ignoriše core_options_path
        # PS2 džojstik preko USB konvertera „Twin USB Joystick“ (0810:0001, dva porta = igrač 1 i 2).
        # Raspored snimljen 20.10.2026: ✕2 ○1 □3 △0, L1 6 R1 7, L2 4 R2 5, SELECT 8, START 9,
        # strelice = osa 0/1 (bez ANALOG-a), u ANALOG režimu strelice su hat. START+SELECT = izlaz iz igre.
        ac = ARC_DIR / 'autoconfig' / 'udev'
        ac.mkdir(parents=True, exist_ok=True)
        pad = {'input_driver': 'udev', 'input_device': 'Twin USB Joystick', 'input_vendor_id': '2064', 'input_product_id': '1',
               'input_b_btn': '2', 'input_a_btn': '1', 'input_y_btn': '3', 'input_x_btn': '0',
               'input_l_btn': '6', 'input_r_btn': '7', 'input_l2_btn': '4', 'input_r2_btn': '5',
               'input_l3_btn': '10', 'input_r3_btn': '11', 'input_select_btn': '8', 'input_start_btn': '9',
               'input_up_axis': '-1', 'input_down_axis': '+1', 'input_left_axis': '-0', 'input_right_axis': '+0',
               'input_up_btn': 'h0up', 'input_down_btn': 'h0down', 'input_left_btn': 'h0left', 'input_right_btn': 'h0right',
               'input_l_x_plus_axis': '+0', 'input_l_x_minus_axis': '-0', 'input_l_y_plus_axis': '+1', 'input_l_y_minus_axis': '-1',
               'input_r_x_plus_axis': '+2', 'input_r_x_minus_axis': '-2', 'input_r_y_plus_axis': '+3', 'input_r_y_minus_axis': '-3'}
        (ac / 'Twin USB Joystick.cfg').write_text(''.join('%s = "%s"\n' % kv for kv in pad.items()))
        opts['joypad_autoconfig_dir'] = str(ARC_DIR / 'autoconfig')
        opts['input_quit_gamepad_combo'] = '4'   # START + SELECT = izlaz iz igre (nazad u Teco)
        opts['input_max_users'] = '2'
        # vibracija: PS1 igre vibriraju samo sa DualShock-om (RETRO_DEVICE_SUBCLASS(ANALOG, 1) = 517).
        # Konverter vibrira kroz hid-pl sa CONFIG_PANTHERLORD_FF — taj modul je napravljen na Pi-ju
        # (~/hid-pl-ff, /lib/modules/<kernel>/updates/hid-pl.ko); posle novog kernela treba ga napraviti ponovo.
        # RetroArch 1.14 tip kontrolera po portu čita iz remap fajla jezgra (ne iz retroarch.cfg)
        rm = ARC_DIR / 'remaps' / 'PCSX-ReARMed'
        rm.mkdir(parents=True, exist_ok=True)
        (rm / 'PCSX-ReARMed.rmp').write_text('input_libretro_device_p1 = "517"\ninput_libretro_device_p2 = "517"\n'
                                             'input_remap_port_p1 = "0"\ninput_remap_port_p2 = "1"\n')
        opts['input_remapping_directory'] = str(ARC_DIR / 'remaps')
        opts['auto_remaps_enable'] = 'true'
        (ARC_DIR / 'retroarch.cfg').write_text('# Teco.Pi Arcade (pravi server.py)\n' + ''.join('%s = "%s"\n' % kv for kv in opts.items()))
        core = {'pcsx_rearmed_drc': 'enabled', 'pcsx_rearmed_spu_interpolation': 'simple', 'pcsx_rearmed_frameskip_type': 'disabled', 'pcsx_rearmed_async_cd': 'async',
                'pcsx_rearmed_neon_enhancement_enable': 'disabled', 'pcsx_rearmed_show_bios_bootlogo': 'disabled',
                'pcsx_rearmed_vibration': 'enabled'}
        if not hdmi:   # PAL TV (NTSC slika je isprana): igra u PAL ritmu 50 fps = 50 Hz ekran, glatko i bez kidanja zvuka
            core['pcsx_rearmed_region'] = 'PAL'   # tačno ovako (velikim slovima), inače jezgro vrati na auto
        (ARC_DIR / 'core-options.cfg').write_text(''.join('%s = "%s"\n' % kv for kv in core.items()))
        return ARC_DIR / 'retroarch.cfg'

    async def arc_start(self, gid):
        g = next((x for x in self.arc_games() if x['id'] == gid), None)
        if not g:
            return {'err': 'Igra nije pronađena (flash priključen?).'}
        core = ARC_CORES / 'pcsx_rearmed_libretro.so'
        if not core.exists() or not shutil.which('retroarch'):
            return {'err': 'Emulator nije instaliran.'}
        if self.source != 'arc':
            await self.set_source('arc')
        if self.tool_running():
            await self.tool_hide(restore=False)
        self.arc_starting = True
        try:
            return await self._arc_launch(g, core)
        finally:
            self.arc_starting = False

    async def _arc_launch(self, g, core):
        await self.arc_stop()
        env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/%d' % os.getuid())
        vol, mute = (100, False) if self.amp_active() else (self.cur_vol(), self.cur_mute())   # pre gašenja mpv-a
        await self.fm_stop()
        await self.stream_stop()
        await self.tv_stop()
        await self.arc_fb(g['name'], 'Pokrećem igru…')   # u prelazu se vidi Arcade, ne Teco.Pi logo
        await self._run('systemctl', '--user', 'stop', 'teco-player', timeout=15)   # mpv oslobađa ekran (DRM) i procesor
        await self.arc_fb(g['name'], 'Pokrećem igru…')
        # oslobodi procesor za emulator: CCTV strim, YouTube Node pomoćnik; procesor na punu brzinu dok se igra
        self.cctv_stop()
        w = getattr(self, 'jsc_worker', None)
        if w and w.p and w.p.poll() is None:
            w.p.kill()
        await self._run('sudo', '-n', 'sh', '-c', 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $f; done', timeout=5)
        log_f = open('/tmp/teco-arcade.log', 'w')
        proc = await asyncio.create_subprocess_exec('retroarch', '--verbose', '--config', str(self.arc_cfg()), '-L', str(core), g['path'],
                                                    stdout=log_f, stderr=asyncio.subprocess.STDOUT, env=env)
        self.arc = {'proc': proc, 'game': g, 't0': time.time(), 'vol': vol, 'mute': mute, 'node': None}
        log.info('Arcade: %s', g['name'])
        loop = asyncio.get_running_loop()
        loop.create_task(self.arc_wait(proc, log_f))
        loop.create_task(self.arc_audio(wait=20))   # jačina sa stranice čim emulator otvori zvuk
        self.mark()
        return {'ok': 'Pokrećem: ' + g['name']}

    def arc_running(self):
        a = getattr(self, 'arc', None)
        return bool(a and a['proc'].returncode is None)

    async def arc_audio(self, wait=0):
        """Jačina/utišano na zvučni tok emulatora (PipeWire), ista skala kao mpv (kubna)."""
        env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/%d' % os.getuid())
        t_end = time.time() + wait
        while self.arc_running():
            a = self.arc
            if not a['node']:
                try:
                    p = await asyncio.create_subprocess_exec('pw-dump', stdout=asyncio.subprocess.PIPE,
                                                             stderr=asyncio.subprocess.DEVNULL, env=env)
                    out, _ = await asyncio.wait_for(p.communicate(), 5)
                    a['node'] = next((o['id'] for o in json.loads(out) if (((o.get('info') or {}).get('props') or {}).get(
                        'application.name') == 'RetroArch' and o['info']['props'].get('media.class') == 'Stream/Output/Audio')), None)
                except (OSError, ValueError, KeyError, asyncio.TimeoutError):
                    pass
            if a['node']:
                n = str(a['node'])
                await self._run('wpctl', 'set-volume', n, '%.3f' % (a['vol'] / 100), timeout=5)   # wpctl je već kubna skala (kao mpv)
                await self._run('wpctl', 'set-mute', n, '1' if a['mute'] else '0', timeout=5)
                return
            if time.time() > t_end:
                return
            await asyncio.sleep(1)

    async def arc_wait(self, proc, log_f):
        """Kad se igra završi (Esc ili Izađi): mpv nazad; YouTube lista se vraća pri izboru YT (yt_snap)."""
        await proc.wait()
        log_f.close()
        a = getattr(self, 'arc', None)
        if a and a.get('proc') is proc:
            await self.arc_restore(a)
            if self.arc is a:
                self.arc = None
                if self.source == 'arc' and self.mpv.connected:   # izašao iz igre (Esc), ostao na Arcade
                    await self.show_arcade()
        log.info('Arcade: kraj igre')
        self.mark()

    def arc_restore(self, a):
        """mpv nazad posle igre — jednom po igri; arc_stop i arc_wait čekaju isti zadatak."""
        if not a.get('rt'):
            a['rt'] = asyncio.get_running_loop().create_task(self._arc_restore(a))
        return a['rt']

    async def _arc_restore(self, a):
        if getattr(self, 'arc_starting', False):   # odmah sledeća igra: mpv ostaje ugašen
            return
        await self._run('sudo', '-n', 'sh', '-c', 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo ondemand > $f; done', timeout=5)
        await self.arc_fb('PlayStation 1', 'Kraj igre')   # dok se plejer podiže: Arcade slika, ne Teco.Pi logo
        self.mpv_back = True   # na povezivanje mpv-a NE sklanjaj sliku (novi izvor je upravo postavlja)
        await self._run('systemctl', '--user', 'start', 'teco-player', timeout=15)
        for _ in range(150):   # čekaj da se mpv poveže pre komandi novog izvora (inače ostane Teco.Pi slika)
            if not self.mpv_back:
                break
            await asyncio.sleep(0.1)
        self.mpv_back = False
        if self.mpv.connected and not self.amp_active():   # novi mpv kreće sa 100%: vrati jačinu iz igre (i utišano)
            await self.mpv.cmd('set_property', 'volume', a.get('vol', 70))
            await self.mpv.cmd('set_property', 'mute', bool(a.get('mute')))
        await self.arc_fb_reset()   # framebuffer opet Teco.Pi logo (za pokretanje i ispade plejera)

    async def arc_stop(self):
        a = getattr(self, 'arc', None)
        if a and a['proc'].returncode is None:
            a['proc'].terminate()   # RetroArch snima SRAM (memorijsku karticu) i izlazi
            try:
                await asyncio.wait_for(a['proc'].wait(), 8)
            except asyncio.TimeoutError:
                a['proc'].kill()
                await a['proc'].wait()
        if a:
            await self.arc_restore(a)

    def arc_snapshot(self):
        a = getattr(self, 'arc', None)
        return {'aspect': self.st.get('arc_aspect', 'wide'), 'games': [dict({k: v for k, v in g.items() if k not in ('path', 'art')}, art=bool(g['art'])) for g in self.arc_games()],
                'run': {'id': a['game']['id'], 'name': a['game']['name'], 't0': a['t0'], 'art': bool(a['game']['art']),
                        'artv': a['game']['artv']} if a and a['proc'].returncode is None else None,
                'bios': getattr(self, '_arc_bios', False)}

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
        return bool(d and not d['bt'] and not d.get('hdmi'))   # pojačalo je samo na analognom izlazu (Speaker)

    def cur_vol(self):
        if self.amp_active():
            return int(self.st.get('amp_vol', 60))
        if self.arc_running():
            return self.arc['vol']
        return int(self.mpv.props.get('volume') or 70)

    def cur_mute(self):
        if self.amp_active():
            return self.amp.muted
        if self.arc_running():
            return self.arc['mute']
        return bool(self.mpv.props.get('mute'))

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
                if name.startswith('Built-in') and 'HDMI' in name:   # ACP HDMI (ekran bez zvuka u EDID-u): ne koristi se, Teco ima svoj
                    continue
                hd = name.startswith('HDMI')   # zvučnici ekrana preko HDMI kabla (teco_hdmi)
                pretty = 'HDMI' if hd else 'Speaker' if name.startswith('Built-in Audio') else name
                sinks.append({'id': int(m.group(2)), 'n': pretty, 'def': m.group(1) == '*', 'bt': not name.startswith('Built-in') and not hd, 'hdmi': hd})
                # jačinu menja samo plejer: izlaz (npr. nove BT slušalice, WirePlumber im da 60%) ide na 100%,
                # jednom po uređaju, da pun zvuk sa stranice ili telefona (Cast) bude zaista pun
                if name not in self.sink_full and m.group(4) and float(m.group(4)) < 1.0:
                    await self._run('wpctl', 'set-volume', m.group(2), '1.0', timeout=5)
                self.sink_full.add(name)
        self.audio = sinks
        streams = out[out.find('Streams:'):out.find('Video')]
        if ('[paused]' in streams or '[init]' in streams) and '[active]' in streams:
            await self.audio_relink()
        if not any(x['hdmi'] for x in sinks) or not getattr(self, '_hdmi_ok', False):
            await self.hdmi_audio_on()
        await self.amp_sync()
        self.mark()

    async def audio_relink(self):
        """Bluetooth slušalice sa dva uređaja (Jabra multipoint): kad drugi uređaj preuzme pa vrati zvuk, PipeWire
        ume da ostavi jednu vezu (npr. desni kanal) zaglavljenu u „paused“ — čuje se samo jedno uvo.
        Takva veza (a druga od istog toka radi) se obriše i napravi ponovo."""
        try:
            dump = json.loads(await self._run('pw-dump', timeout=8) or '[]')
        except ValueError:
            return
        links = [o for o in dump if o.get('type', '').endswith(':Link')]
        active = {(o['info'].get('output-node-id'), o['info'].get('input-node-id')) for o in links if o['info'].get('state') == 'active'}
        for o in links:
            i = o['info']
            if i.get('state') in ('paused', 'init') and (i.get('output-node-id'), i.get('input-node-id')) in active:
                log.info('zvuk: zaglavljena veza %s (kanal) — pravim ponovo', o['id'])
                await self._run('pw-link', '-d', str(o['id']), timeout=5)
                await self._run('pw-link', str(i.get('output-port-id')), str(i.get('input-port-id')), timeout=5)

    # ---------- Bluetooth slušalice sa dva uređaja (multipoint): drugi uređaj preuzme zvuk -> YouTube pauza,
    # oslobodi ga -> nastavak. Stanje se čita iz BlueZ prenosa (MediaTransport1.State: active / idle / pending).
    async def bt_transport_state(self, mac):
        """Stanje prenosa ka slušalicama (active / idle / pending). Putanja (…/sepN/fdN) se pamti — menja se samo
        kad se veza obnovi, pa se tada potraži ponovo (proverava se na 0,4 s dok svira)."""
        for retry in (False, True):
            path = getattr(self, '_bt_tpath', None)
            if retry or not path or mac.replace(':', '_') not in path:
                tree = await self._run('busctl', 'tree', 'org.bluez', '--list', timeout=5)
                path = self._bt_tpath = next((l.strip() for l in tree.splitlines() if re.search(r'/dev_%s/sep\d+/fd\d+$' % mac.replace(':', '_'), l)), None)
                if not path:
                    return None
            out = await self._run('busctl', 'get-property', 'org.bluez', path, 'org.bluez.MediaTransport1', 'State', timeout=5)
            m = re.search(r'"(\w+)"', out)
            if m:
                return m.group(1)
        return None

    async def bt_profile_cycle(self, mac):
        """Najpouzdaniji oporavak BT zvuka (zaglavljen kanal, uspavan izlaz): profil off pa a2dp-sink; mpv se sam ponovo poveže."""
        try:
            dump = json.loads(await self._run('pw-dump', timeout=8) or '[]')
        except ValueError:
            return
        name = 'bluez_card.' + mac.replace(':', '_')
        for o in dump:
            p = (o.get('info') or {}).get('props') or {}
            if p.get('device.name') == name:
                prof = {x.get('name'): x.get('index') for x in (o['info'].get('params') or {}).get('EnumProfile', [])}
                if 'off' in prof and 'a2dp-sink' in prof:
                    log.info('Bluetooth: obnavljam zvuk na slušalicama (profil)')
                    await self._run('wpctl', 'set-profile', str(o['id']), str(prof['off']), timeout=5)
                    await asyncio.sleep(0.8)
                    await self._run('wpctl', 'set-profile', str(o['id']), str(prof['a2dp-sink']), timeout=5)
                    for _ in range(16):   # sačekaj da se izlaz slušalica vrati i bude glavni (inače zvuk ode na HDMI)
                        await asyncio.sleep(0.5)
                        await self.audio_refresh()
                        if any(x['bt'] and x['def'] for x in self.audio):
                            break

    async def bt_channels_ok(self, mac):
        """Posle vraćanja zvuka: oba kanala moraju da rade. Prvo brzo ponovno povezivanje veze, a ako i to ne pomogne — obnova profila."""
        for attempt in range(4):
            await asyncio.sleep(0.5)
            out = await self._run('wpctl', 'status', timeout=5)
            streams = out[out.find('Streams:'):out.find('Video')]
            if not (('[paused]' in streams or '[init]' in streams) and '[active]' in streams):
                return
            if attempt < 3:
                await self.audio_relink()
            else:
                await self.bt_profile_cycle(mac)

    async def bt_guard_loop(self):
        """Slušalice sa dva uređaja (Jabra multipoint): drugi uređaj preuzme zvuk (poruka, poziv, muzika) ->
        YouTube pauza + par sekundi unazad. Zatim pokušaji povratka (posle 2, 4, 6, 10, 15, 30 s, uz obnovu veze
        (kao ručno prebacivanje izlaza pa nazad na Bluetooth) i puštanje; ako slušalice prihvate Pi i zvuk ostane
        >= 2,5 s — nastavlja, inače opet pauza + unazad i kasniji pokušaj. Kratki zvuci (Teams) tako ne prekidaju dugo.
        (Jabra dok telefon svira ume da prihvati Pi na 1-4 s pa ga odbaci, zato se traži 2,5 s bez prekida.)"""
        WAITS = (2, 4, 6, 10, 15, 30)
        silence = Path('/tmp/teco-silence.wav')   # 5 s tišine za probu
        if not silence.exists():
            import wave
            with wave.open(str(silence), 'wb') as w:
                w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000); w.writeframes(b'\0' * 48000 * 4 * 5)
        idle_for, hold, seen_active, tick, tries, next_try = 0, False, False, 1, 0, 0

        async def pause_back(sec):
            await self.mpv.cmd('set_property', 'pause', True)
            await self.mpv.cmd('seek', -max(2, int(sec)), 'relative')   # ništa se ne propusti
            self.mark()

        while True:
            await asyncio.sleep(tick)
            tick = 1
            try:
                bt_def = next((x for x in self.audio if x['def'] and x['bt']), None)
                dev = next((d for d in self.bt.get('devs', []) if d.get('conn') and d.get('audio')), None)
                if not bt_def or not dev or self.mode != 'yt' or self.source != 'av1':
                    idle_for, hold, seen_active = 0, False, False
                    continue
                playing = not self.mpv.props.get('pause')
                if hold:
                    if playing:   # korisnik je sam pritisnuo play: obnovi vezu pa nastavi
                        hold = False
                        await self.mpv.cmd('set_property', 'pause', True)
                        await self.bt_profile_cycle(dev['mac'])
                        await self.mpv.cmd('set_property', 'pause', False)
                        await self.bt_channels_ok(dev['mac'])
                        continue
                    if tries >= len(WAITS) or time.monotonic() < next_try:
                        continue
                    tries += 1
                    await self.bt_profile_cycle(dev['mac'])   # bez obnove veze Jabra posle preuzimanja ne prihvata Pi
                    # proba tišinom (video ostaje pauziran, bez skakanja play/pauza): slobodne su tek ako
                    # prihvate Pi bez prekida >= 2,5 s
                    probe = await asyncio.create_subprocess_exec('pw-play', '--target', str(next((x['id'] for x in self.audio if x['def'] and x['bt']), bt_def['id'])), str(silence),
                                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                                                                 env=dict(os.environ, XDG_RUNTIME_DIR='/run/user/%d' % os.getuid()))
                    t0, ok, run = time.monotonic(), 0, 0
                    while time.monotonic() - t0 < 5:
                        await asyncio.sleep(0.25)
                        run = run + 0.25 if await self.bt_transport_state(dev['mac']) == 'active' else 0
                        if run >= 2.5:
                            ok = 1
                            break
                    try:
                        probe.kill()
                    except ProcessLookupError:   # pw-play je već završio
                        pass
                    await probe.wait()
                    if ok:
                        hold, seen_active, idle_for = False, False, 0
                        await self.mpv.cmd('set_property', 'pause', False)
                        self.mark()
                        log.info('Bluetooth: %s ponovo slobodan — YouTube nastavlja (pokušaj %d)', dev['n'], tries)
                        await self.bt_channels_ok(dev['mac'])
                    else:
                        next_try = time.monotonic() + (WAITS[tries] if tries < len(WAITS) else 0)
                        log.info('Bluetooth: %s još zauzet (pokušaj %d)', dev['n'], tries)
                    continue
                if not playing:
                    idle_for, seen_active = 0, False
                    continue
                tick = 0.4
                st = await self.bt_transport_state(dev['mac'])
                if st == 'active':
                    seen_active, idle_for = True, 0
                    continue
                if not seen_active:   # tek pušteno: slušalicama treba 1-2 s da prihvate zvuk
                    continue
                idle_for += 1
                if idle_for >= 3:   # ~1,2 s bez prenosa (a pre toga je radio): slušalice je preuzeo drugi uređaj
                    await pause_back(3)
                    hold, idle_for, seen_active, tries = True, 0, False, 0
                    next_try = time.monotonic() + WAITS[0]
                    log.info('Bluetooth: %s preuzeo drugi uređaj — YouTube pauziran', dev['n'])
            except Exception as e:
                log.warning('Bluetooth nadzor: %r', e)
                await asyncio.sleep(5)
    HDMI_SINK = 'teco_hdmi'

    async def hdmi_audio_on(self):
        """Zvučnici ekrana preko HDMI-ja. Ekran u EDID-u ne javlja zvuk, pa ga WirePlumber (ACP) vodi kao „nedostupan“
        i nikad ga ne koristi. Zato Teco pravi svoj PipeWire izlaz direktno na hdmi: ALSA uređaj (radi i IEC958 pakovanje),
        a ACP profil kartice drži ugašen (isti uređaj ne mogu oba). Najviše jednom u minuti, samo kad je HDMI priključen.
        (Ne u pipewire.conf: greška tamo obori ceo zvuk.)"""
        if time.monotonic() - getattr(self, '_hdmi_try', -999) < 60:
            return
        self._hdmi_try = time.monotonic()
        try:
            if Path('/sys/class/drm/card0-HDMI-A-1/enabled').read_text().strip() != 'enabled':
                return
            card = next(l.split()[0] for l in Path('/proc/asound/cards').read_text().splitlines() if 'vc4hdmi' in l)
        except (OSError, StopIteration):
            return
        try:
            dump = json.loads(await self._run('pw-dump', timeout=8) or '[]')
        except ValueError:
            return
        # već postoji: tačno jedan, sa isključenim gašenjem u mirovanju -> ništa. Duplikati (ranije se pravio
        # više puta) i stari bez tog podešavanja se brišu i pravi se jedan nov.
        mine = [o for o in dump if o.get('type') == 'PipeWire:Interface:Node' and ((o.get('info') or {}).get('props') or {}).get('node.name') == self.HDMI_SINK]
        if len(mine) == 1 and str(mine[0]['info']['props'].get('session.suspend-timeout-seconds')) == '0':
            self._hdmi_ok = True
            return
        for o in mine:
            await self._run('pw-cli', 'destroy', str(o['id']), timeout=5)
        for o in dump:   # ACP profil kartice ugasi (oslobađa uređaj)
            p = (o.get('info') or {}).get('props') or {}
            if p.get('media.class') == 'Audio/Device' and p.get('api.alsa.card.name') == 'vc4-hdmi':
                if any(x.get('name') != 'off' for x in (o['info'].get('params') or {}).get('Profile', [])):
                    await self._run('wpctl', 'set-profile', str(o['id']), '0', timeout=5)
        props = ('{ factory.name=api.alsa.pcm.sink node.name=%s node.description="HDMI (ekran)" node.nick=HDMI media.class=Audio/Sink '
                 'api.alsa.path="hdmi:CARD=vc4hdmi,DEV=0" api.alsa.pcm.card=%s audio.channels=2 audio.position=[FL FR] object.linger=true '
                 # bez gašenja u mirovanju: ekranu posle buđenja HDMI zvuka treba ~1 s da ga uhvati (prvi sekund izobličen)
                 'session.suspend-timeout-seconds=0 node.pause-on-idle=false }') % (self.HDMI_SINK, card)
        await self._run('pw-cli', 'create-node', 'adapter', props, timeout=8)
        self._hdmi_ok = True
        log.info('HDMI zvuk: izlaz napravljen')
        try:   # kratka tišina: otvori izlaz odmah (posle ostaje otvoren), da ni prvo zvono ne bude izobličeno
            sil = EVENTS_DIR / '.silence.wav'
            if not sil.exists():
                import wave
                with wave.open(str(sil), 'wb') as w:
                    w.setnchannels(1), w.setsampwidth(2), w.setframerate(48000), w.writeframes(b'\0\0' * 9600)
            await asyncio.sleep(1)
            await self.play_sound(sil)
        except Exception as e:
            log.warning('HDMI tišina: %s', e)
        if (self.st.get('audio_out') == 'hdmi'):   # bio je izabran pre restarta zvuka / Pi-ja
            await asyncio.sleep(1)
            await self._run('pw-metadata', '0', 'default.configured.audio.sink', '{ "name": "%s" }' % self.HDMI_SINK, 'Spa:String:JSON', timeout=5)
    async def set_output(self, sid):
        """Glavni izlaz zvuka preko „default“ metapodataka (po imenu čvora). wpctl set-default pada
        kad postoji izlaz koji je napravio Teco (HDMI), pa se ne koristi."""
        s = next((x for x in self.audio if x['id'] == int(sid)), None)
        self.st['audio_out'] = 'hdmi' if s and s.get('hdmi') else 'other'
        save_state(self.st)
        name = self.HDMI_SINK if s and s.get('hdmi') else None
        if not name:
            try:
                o = next((x for x in json.loads(await self._run('pw-dump', str(int(sid)), timeout=5) or '[]') if x.get('id') == int(sid)), {})
                name = ((o.get('info') or {}).get('props') or {}).get('node.name')
            except ValueError:
                name = None
        if name:
            await self._run('pw-metadata', '0', 'default.configured.audio.sink', '{ "name": "%s" }' % name, 'Spa:String:JSON', timeout=5)
        else:
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
        if c == 'bt_connect' and not any(d['mac'] == mac and d['paired'] for d in self.bt.get('devs', [])):
            return {'err': 'Uređaj nije uparen. Novi uređaji se uparuju u Adminu.'}
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
            name = next((d['n'] for d in self.bt.get('devs', []) if d['mac'] == mac), mac)
            for d in self.bt.get('devs', []):   # povezan može biti samo jedan uređaj
                if d['conn'] and d['mac'] != mac:
                    await self._run('bluetoothctl', 'disconnect', d['mac'], timeout=10)
            sink = None
            # Pi 3B: Wi-Fi i Bluetooth dele radio, pa prvo povezivanje ume da ne uspe (page timeout), a slušalice
            # (Jabra) ponude zvuk tek par sekundi posle povezivanja: 2 pokušaja i čekanje izlaza do ~12 s
            for attempt in range(2):
                info = await self._run('bluetoothctl', 'info', mac, timeout=5)
                if 'Connected: yes' not in info:
                    await self._run('bluetoothctl', '--agent', 'NoInputNoOutput', '--timeout', '12', 'connect', mac, timeout=20)
                    if 'Connected: yes' not in await self._run('bluetoothctl', 'info', mac, timeout=5):
                        await asyncio.sleep(2)   # nije se povezao: odmah sledeći pokušaj, bez čekanja zvuka
                        continue
                for _ in range(12):
                    await self.audio_refresh()
                    sink = next((s for s in self.audio if s['bt'] and s['n'] == name), None) \
                        or next((s for s in self.audio if s['bt']), None)   # izlaz nosi ime uređaja (npr. „Jabra Damir“)
                    if sink:
                        break
                    await asyncio.sleep(1)
                if sink:
                    break
                log.info('Bluetooth %s: bez izlaza za zvuk, ponovo povezujem', name)
                await self._run('bluetoothctl', 'disconnect', mac, timeout=10)
                await asyncio.sleep(2)
            await self.bt_refresh()
            if sink:
                await self.set_output(sink['id'])   # zvuk odmah prebaci na slušalice
                return {'ok': 'Zvuk ide na ' + sink['n']}
            connected = 'Connected: yes' in await self._run('bluetoothctl', 'info', mac, timeout=5)
            return {'err': name + ': povezano, ali ne nudi zvuk (probaj ponovo ili ugasi/upali slušalice).' if connected
                    else name + ': povezivanje nije uspelo (uključene? u dometu?).'}
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

    # ---------- Smart Home uređaji: registar + drajveri po proizvođaču (DEV_DRIVERS). Svaki drajver zna
    # probe (da li uređaj odgovara), poll (stanje), set (upali/ugasi, jačina) i scan (pretraga u mreži).
    # Za sada WiZ (lokalni UDP API, port 38899); Sonoff/Shelly/Tasmota se dodaju kao nove klase.
    def dev_snapshot(self):
        """Za stranicu: bez lozinki i adresa sa lozinkom kamera (cam ostaje na Pi-ju)."""
        ds = getattr(self, 'dev_state', {})
        out = []
        for d in self.st.get('devices', []):
            x = {k: v for k, v in d.items() if k != 'cam'}
            if d.get('cam'):
                x['cam'] = {'user': d['cam'].get('user') or '', 'ch': d['cam'].get('ch') or 1, 'has_pass': bool(d['cam'].get('pass'))}
            st = dict(ds.get(d['id'], {'ok': False}))
            if d.get('type') == 'door':   # vrata: stanje iz zone alarma (ako je izabrana), PGM uključen
                dr, al = d.get('door') or {}, getattr(self, 'alarm', None) or {}
                z = next((z for z in al.get('zones', []) if dr.get('zone') and z.get('id') == int(dr['zone'])), None)
                g = next((g for g in al.get('pgms', []) if g.get('id') == int(dr.get('pgm') or 0)), None)
                st = {'ok': al.get('run') == 'run', 'open': z.get('open') if z else None, 'zone_label': z.get('label') if z else None,
                      'pgm_on': bool(g and g.get('on')) or time.time() < getattr(self, 'door_until', {}).get(d['id'], 0)}
            if d.get('proto') == 'applpgm':   # uređaj na PGM releju: uključen = PGM uključen
                al = getattr(self, 'alarm', None) or {}
                g = next((g for g in al.get('pgms', []) if g.get('id') == int((d.get('appl') or {}).get('pgm') or 0)), None)
                st = {'ok': al.get('run') == 'run', 'on': bool(g and g.get('on'))}
            if d.get('type') in RING_TYPES:   # interfon / zvono: zvoni (zona otvorena sada) i poslednji poziv
                al = getattr(self, 'alarm', None) or {}
                zid = int((d.get('intercom') or {}).get('zone') or 0)
                z = next((z for z in al.get('zones', []) if z.get('id') == zid), None)
                st = {'ok': al.get('run') == 'run', 'ringing': bool(z and z.get('open')), 'zone_label': z.get('label') if z else None,
                      'last_ring': getattr(self, 'ring_last', {}).get(d['id'])}
            out.append(dict(x, **st))
        return out

    @staticmethod
    def door_fields(a, old=None):
        dr = dict((old or {}).get('door') or {})
        try:
            if a.get('door_pgm') not in (None, ''):
                dr['pgm'] = max(1, min(32, int(a['door_pgm'])))
            if a.get('door_zone') is not None:
                dr['zone'] = int(a['door_zone']) if str(a['door_zone']).strip() not in ('', '0') else None
        except (TypeError, ValueError):
            return None, 'PGM i zona su brojevi.'
        if a.get('door_mode') in ('pulse', 'toggle'):
            dr['mode'] = a['door_mode']
        if not dr.get('pgm'):
            return None, 'Izaberi PGM izlaz.'
        dr.setdefault('mode', 'pulse')
        return dr, None

    @staticmethod
    def cam_fields(a, drv, old=None):
        """Podaci kamere iz forme: korisnik, lozinka (prazno = ostaje stara), kanal, ili cela adresa (Drugi)."""
        from urllib.parse import urlparse
        cam = dict((old or {}).get('cam') or {})
        if drv.key == 'camurl':
            url = str(a.get('cam_url') if a.get('cam_url') is not None else cam.get('url', '')).strip()
            u = urlparse(url)
            if u.scheme not in ('rtsp', 'rtsps', 'http', 'https') or not u.hostname:
                return None, None, 'Adresa kamere treba da počinje sa rtsp:// ili http(s)://'
            cam['url'] = url
            return cam, u.hostname, None
        for k in ('user', 'ch', 'port'):
            if a.get('cam_' + k) not in (None, ''):
                cam[k] = str(a['cam_' + k]).strip()[:40]
        if a.get('cam_pass'):
            cam['pass'] = str(a['cam_pass'])[:64]
        return cam, None, None

    async def dev_poll(self, d):
        drv = DEV_DRIVERS.get(d.get('proto'))
        st = await drv.poll(d) if drv and drv.ready else {'ok': False}
        if not hasattr(self, 'dev_state'):
            self.dev_state = {}
        fails = self.__dict__.setdefault('dev_fails', {})
        if st.get('ok'):
            fails.pop(d['id'], None)
        elif (self.dev_state.get(d['id']) or {}).get('ok'):   # „ne odgovara“ tek posle 3 neuspele provere zaredom (~15 s)
            fails[d['id']] = fails.get(d['id'], 0) + 1
            if fails[d['id']] < 3:
                return
        prev = self.dev_state.get(d['id'])
        if d.get('type') == 'tv' and st.get('on') and prev is not None and prev.get('ok') and not prev.get('on') and hasattr(self, 'mpv_kick'):
            self.mpv_kick.set()   # TV se upravo upalio: proveri da li se plejer zamrznuo
        if self.dev_state.get(d['id']) != st:
            self.dev_state[d['id']] = st
            self.mark()

    # ---------- Tajmer: zvono (odbrojavanje isteklo / alarm u HH:MM) — melodija preko zvuka Teco.Pi-ja (TV)
    ALARM_WAV = DATA / 'alarm.wav'

    def timer_wav(self):
        """Kratka melodija (C-E-G-C, dva puta, pauza) kao WAV; pravi se jednom."""
        if self.ALARM_WAV.exists():
            return str(self.ALARM_WAV)
        import math
        import struct
        import wave
        sr, out = 22050, bytearray()
        for f, d in [(523, .16), (659, .16), (784, .16), (1047, .32), (0, .1)] * 2 + [(0, .5)]:
            n = int(sr * d)
            for i in range(n):
                env = min(1, i / 300, (n - i) / 900) if f else 0
                out += struct.pack('<h', int(9000 * env * (math.sin(2 * math.pi * f * i / sr) + 0.3 * math.sin(4 * math.pi * f * i / sr))))
        DATA.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.ALARM_WAV), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(bytes(out))
        return str(self.ALARM_WAV)

    def timer_sound(self, on):
        t = getattr(self, 'timer_snd', None)
        if t and not t.done():
            t.cancel()
        self.timer_snd = None
        if not on:
            return

        async def play():   # melodija se ponavlja do 2 min ili dok se ne ugasi
            wav = await asyncio.get_running_loop().run_in_executor(None, self.timer_wav)
            end = time.time() + 120
            while time.time() < end:
                p = await asyncio.create_subprocess_exec('pw-play', wav, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                try:
                    await p.wait()
                except asyncio.CancelledError:
                    p.kill()
                    raise
                await asyncio.sleep(0.3)
        self.timer_snd = asyncio.get_running_loop().create_task(play())

    async def timer_loop(self):
        """Na 1 s: isteklo odbrojavanje ili došlo vreme alarma → zvono (melodija + „ALARM“ na stranici i TV-u)."""
        while True:
            await asyncio.sleep(1)
            try:
                c = self.tool_cfg('timer')
                now, ring = time.time(), None
                if c['run'] and 0 < c['end'] <= now and c.get('cd_rang') != c['end']:
                    ring, c['cd_rang'] = 'cd', c['end']
                if c.get('al_on'):
                    lt = time.localtime(now)
                    key = time.strftime('%Y-%m-%d ', lt) + c['al_at']
                    if time.strftime('%H:%M', lt) == c['al_at'] and c.get('al_last') != key:
                        ring, c['al_last'] = 'al', key
                if ring:
                    c['ring'] = now
                    c['ring_kind'] = ring
                    self.st.setdefault('tools', {})['timer'] = c
                    save_state(self.st)
                    self.tool_write('timer')
                    self.timer_sound(True)
                    log.info('tajmer: %s', 'isteklo odbrojavanje' if ring == 'cd' else 'alarm ' + c['al_at'])
                    self.mark()
            except Exception:
                log.exception('tajmer')

    @staticmethod
    async def mpv_probe(timeout=5):
        """Nova veza na mpv socket + komanda koja ide kroz jezgro; True ako odgovori na vreme."""
        try:
            r, w = await asyncio.wait_for(asyncio.open_unix_connection('/tmp/mpvsocket'), timeout)
        except (OSError, asyncio.TimeoutError):
            return None
        try:
            w.write(b'{"command":["show-text","",1],"request_id":424242}\n')
            await asyncio.wait_for(w.drain(), timeout)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                line = await asyncio.wait_for(r.readline(), max(0.1, end - time.monotonic()))
                if not line:
                    return None
                if b'"request_id":424242' in line:
                    return True
            return False
        except asyncio.TimeoutError:
            return False
        except OSError:
            return None
        finally:
            w.close()

    async def mpv_guard(self):
        """Plejer (mpv) se posle gašenja/paljenja TV-a ume zamrznuti: slika stoji, alati i video se ne vide.
        Na 15 s (i odmah kad se TV upali) proveri da li odgovara; dva puta zaredom ne → restart plejera."""
        self.mpv_kick = asyncio.Event()
        fails = 0
        log.info('nadzor plejera pokrenut')
        while True:
            try:
                await asyncio.wait_for(self.mpv_kick.wait(), 15)
                self.mpv_kick.clear()
                # TV se upravo upalio: plejer posle gašenja TV-a ostane zamrznut (slika stoji, alati se ne vide),
                # a provera to ne vidi uvek — zato ga posle paljenja TV-a uvek osveži (sem kad radi Arcade)
                await asyncio.sleep(3)
                if not self.arc_running():
                    log.info('TV upaljen: osvežavam plejer (mpv)')
                    await self._run('systemctl', '--user', 'restart', 'teco-player', timeout=30)
                    await asyncio.sleep(10)
                continue
            except asyncio.TimeoutError:
                pass
            try:
                if self.arc_running():   # Arcade: plejer je namerno ugašen
                    fails = 0
                    continue
                ok = await self.mpv_probe() if self.mpv.connected else None   # nova veza, kao što se alati povezuju
                fails = fails + 1 if ok is False else 0
                if ok is not True:
                    log.info('nadzor plejera: %s (%d)', 'ne odgovara' if ok is False else 'nije povezan', fails)
                if fails >= 2:
                    fails = 0
                    log.warning('plejer (mpv) ne odgovara — verovatno posle gašenja TV-a; restartujem ga')
                    await self._run('systemctl', '--user', 'restart', 'teco-player', timeout=30)
                    await asyncio.sleep(20)
            except Exception:
                log.exception('mpv_guard')

    async def dev_loop(self):
        """Stanje uređaja na 5 s; komanda sa stranice osveži odmah."""
        old = DATA / 'lgtv-key.json'   # LG TV iz ranije verzije (posebna strana): sada je običan uređaj
        if old.exists():
            ip = (self.st.get('lgtv') or {}).get('ip') or '172.10.10.140'
            os.replace(old, LgTvDriver.key_file(ip))
            devs = self.st.setdefault('devices', [])
            if not any(x['ip'] == ip for x in devs):
                devs.append({'id': secrets.token_hex(4), 'type': 'tv', 'proto': 'lgwebos', 'ip': ip,
                             'mac': (self.st.get('lgtv') or {}).get('mac') or '74:e6:b8:a3:dc:46', 'name': 'TV', 'room': '', 'pi_in': 'HDMI_1'})
            self.st.pop('lgtv', None)
            save_state(self.st)
        while True:
            for d in list(self.st.get('devices', [])):
                try:
                    await self.dev_poll(d)
                except Exception as e:
                    log.warning('uređaj %s: %s', d.get('name'), e)
            await asyncio.sleep(5)

    @staticmethod
    def dev_fields(a, d=None):
        """Naziv, prostorija, IP iz forme (za dodavanje i izmenu)."""
        import ipaddress
        try:
            ip = str(ipaddress.IPv4Address(str(a.get('ip') if a.get('ip') is not None else (d or {}).get('ip', '')).strip()))
        except ValueError:
            return None, 'IP adresa nije ispravna.'
        name = str(a.get('name') if a.get('name') is not None else (d or {}).get('name', '')).strip()[:40]
        room = str(a.get('room') if a.get('room') is not None else (d or {}).get('room', '')).strip()[:30]
        return {'ip': ip, 'name': name, 'room': room}, None

    async def dev_cmd(self, c, a):
        devs = self.st.setdefault('devices', [])
        if c == 'dev_scan':
            drv = DEV_DRIVERS.get(a.get('proto') or 'wiz')
            if not drv or not drv.ready:
                return {'err': 'Pretraga za ovog proizvođača još ne postoji.'}
            have = {d['ip'] for d in devs}
            found = await drv.scan()
            for x in found:
                x['added'] = x['ip'] in have
            return {'found': sorted(found, key=lambda x: x['ip'])}
        if c == 'dev_add':
            drv = DEV_DRIVERS.get(a.get('proto') or 'wiz')
            if not drv or not drv.ready:
                return {'err': (drv.label if drv else 'Ovaj proizvođač') + ' još nije podržan.'}
            if drv.dtype in RING_TYPES:   # interfon / zvono: zona alarma kao okidač, obaveštenje koje se pokreće
                try:
                    zone = int(a.get('ic_zone') or 0)
                except (TypeError, ValueError):
                    zone = 0
                if not zone:
                    return {'err': 'Izaberi zonu alarma koja se okida kad neko zvoni.'}
                ev0 = 'zvono' if drv.dtype == 'bell' else 'interfon'
                d = {'id': secrets.token_hex(4), 'type': drv.dtype, 'proto': drv.key, 'ip': '',
                     'intercom': {'zone': zone, 'event': a.get('ic_event') if a.get('ic_event') in self.st.get('events', {}) else ev0},
                     'name': str(a.get('name') or '').strip()[:40] or DEV_TYPES[drv.dtype], 'room': str(a.get('room') or '').strip()[:30]}
                devs.append(d)
                save_state(self.st)
                self.mark()
                return {'ok': 'Dodato: ' + d['name']}
            if drv.key == 'usbcam':   # USB kamera na Pi-ju: bez IP adrese, bira se sa liste priključenih
                devp = str(a.get('usb_dev') or '')
                cams = UsbCamDriver.list_devices()
                if not cams:
                    return {'err': 'Nijedna USB kamera nije priključena.'}
                if devp not in [c['dev'] for c in cams]:
                    devp = cams[0]['dev']
                d = {'id': secrets.token_hex(4), 'type': 'camera', 'proto': drv.key, 'ip': '', 'cam': {'dev': devp},
                     'name': str(a.get('name') or '').strip()[:40] or next(c['n'] for c in cams if c['dev'] == devp),
                     'room': str(a.get('room') or '').strip()[:30]}
                devs.append(d)
                save_state(self.st)
                await self.dev_poll(d)
                self.mark()
                return {'ok': 'Dodato: ' + d['name']}
            kind = a.get('appl_kind') if a.get('appl_kind') in APPL_KINDS else 'other'
            if drv.key == 'applpgm':   # uređaj na PGM releju alarma: bez IP adrese
                try:
                    pgm = max(1, min(32, int(a.get('appl_pgm') or 0)))
                except (TypeError, ValueError):
                    return {'err': 'Izaberi PGM izlaz.'}
                d = {'id': secrets.token_hex(4), 'type': 'appliance', 'proto': drv.key, 'ip': '', 'appl': {'kind': kind, 'pgm': pgm},
                     'name': str(a.get('name') or '').strip()[:40] or APPL_KINDS[kind], 'room': str(a.get('room') or '').strip()[:30]}
                devs.append(d)
                save_state(self.st)
                self.mark()
                return {'ok': 'Dodato: ' + d['name']}
            if drv.dtype == 'door':   # vrata preko PGM-a alarma: bez IP adrese
                dr, err = self.door_fields(a)
                if err:
                    return {'err': err}
                d = {'id': secrets.token_hex(4), 'type': 'door', 'proto': drv.key, 'ip': '', 'door': dr,
                     'name': str(a.get('name') or '').strip()[:40] or 'Vrata', 'room': str(a.get('room') or '').strip()[:30]}
                devs.append(d)
                save_state(self.st)
                self.mark()
                return {'ok': 'Dodato: ' + d['name']}
            cam = None
            if drv.dtype == 'camera':
                cam, host, err = self.cam_fields(a, drv)
                if err:
                    return {'err': err}
                if host:   # Drugi (adresa): IP/host iz adrese
                    a = dict(a, ip=host)
            f, err = self.dev_fields(a) if not (cam and drv.key == 'camurl') else ({'ip': a['ip'], 'name': str(a.get('name') or '').strip()[:40], 'room': str(a.get('room') or '').strip()[:30]}, None)
            if err:
                return {'err': err}
            if drv.dtype != 'camera' and any(d['ip'] == f['ip'] for d in devs):
                return {'err': 'Uređaj sa tom adresom već postoji.'}
            tmp = dict(f, id='', cam=cam)
            info = await (drv.probe(f['ip'], tmp) if drv.dtype == 'camera' else drv.probe(f['ip']))
            if info is None:
                return {'err': '%s na %s ne odgovara.' % (drv.label, f['ip'])}
            d = dict(f, id=secrets.token_hex(4), type=drv.dtype, proto=drv.key, mac=info.get('mac'),
                     name=f['name'] or (APPL_KINDS[kind] if drv.dtype == 'appliance' else DEV_TYPES.get(drv.dtype, 'Uređaj')))
            if cam is not None:
                d['cam'] = cam
            if drv.dtype == 'appliance':
                d['appl'] = {'kind': kind}
            devs.append(d)
            save_state(self.st)
            await self.dev_poll(d)
            self.mark()
            return {'ok': 'Dodato: ' + d['name']}
        d = next((x for x in devs if x['id'] == a.get('id')), None)
        if not d:
            return {'err': 'Uređaj nije pronađen.'}
        if c == 'dev_edit' and d.get('proto') == 'usbcam':
            if a.get('usb_dev') and a['usb_dev'] in [x['dev'] for x in UsbCamDriver.list_devices()]:
                d['cam'] = {'dev': a['usb_dev']}
                self.cctv_stop(d['id'])
            d.update(name=str(a.get('name') or d['name']).strip()[:40], room=str(a.get('room') if a.get('room') is not None else d.get('room', '')).strip()[:30])
            save_state(self.st)
            await self.dev_poll(d)
            self.mark()
            return {'ok': 'Sačuvano: ' + d['name']}
        if c == 'dev_edit' and d.get('type') == 'appliance':   # vrsta (klima, bojler…) i PGM; IP ide dalje kao kod ostalih
            ap = dict(d.get('appl') or {})
            if a.get('appl_kind') in APPL_KINDS:
                ap['kind'] = a['appl_kind']
            if d.get('proto') == 'applpgm':
                try:
                    if a.get('appl_pgm') not in (None, ''):
                        ap['pgm'] = max(1, min(32, int(a['appl_pgm'])))
                except (TypeError, ValueError):
                    return {'err': 'PGM je broj.'}
                d.update(appl=ap, name=str(a.get('name') or d['name']).strip()[:40], room=str(a.get('room') if a.get('room') is not None else d.get('room', '')).strip()[:30])
                save_state(self.st)
                self.mark()
                return {'ok': 'Sačuvano: ' + d['name']}
            d['appl'] = ap
        if c == 'dev_set' and d.get('proto') == 'applpgm':   # relej uređaja: PGM uključi / isključi
            r = await self.alarm_do({'pgm': str((d.get('appl') or {}).get('pgm')), 'cmd': 'on' if a.get('on') else 'off'})
            self.mark()
            return {'err': r['err']} if r.get('err') else {'ok': d['name'] + (': uključeno' if a.get('on') else ': isključeno')}
        if c == 'dev_edit' and d.get('type') in RING_TYPES:
            ic = dict(d.get('intercom') or {})
            try:
                if a.get('ic_zone') not in (None, ''):
                    ic['zone'] = int(a['ic_zone'])
            except (TypeError, ValueError):
                return {'err': 'Zona je broj.'}
            if a.get('ic_event') in self.st.get('events', {}):
                ic['event'] = a['ic_event']
            d.update(intercom=ic, name=str(a.get('name') or d['name']).strip()[:40], room=str(a.get('room') if a.get('room') is not None else d.get('room', '')).strip()[:30])
            save_state(self.st)
            self.mark()
            return {'ok': 'Sačuvano: ' + d['name']}
        if c == 'dev_edit' and d.get('type') == 'door':
            dr, err = self.door_fields(a, d)
            if err:
                return {'err': err}
            d.update(door=dr, name=str(a.get('name') or d['name']).strip()[:40], room=str(a.get('room') if a.get('room') is not None else d.get('room', '')).strip()[:30])
            save_state(self.st)
            self.mark()
            return {'ok': 'Sačuvano: ' + d['name']}
        if c == 'dev_set' and d.get('type') == 'door':   # otvori (impuls 5 s) ili uključi / isključi PGM
            dr = d.get('door') or {}
            if a.get('stop'):   # dugo držanje: ugasi relej odmah (PGM nazad na normalan rad)
                getattr(self, 'door_until', {}).pop(d['id'], None)
                r = await self.alarm_do({'pgm': str(dr.get('pgm')), 'cmd': 'release'})
                self.mark()
                return {'err': r['err']} if r.get('err') else {'ok': d['name'] + ': relej ugašen'}
            cmd = 'pulse' if dr.get('mode', 'pulse') == 'pulse' else ('on' if a.get('on') else 'off')
            r = await self.alarm_do({'pgm': str(dr.get('pgm')), 'cmd': cmd})
            if not r.get('err') and cmd == 'pulse':   # centrala PGM ne javlja odmah: server pamti impuls 5 s i prikazuje ga svima
                if not hasattr(self, 'door_until'):
                    self.door_until = {}
                self.door_until[d['id']] = time.time() + 2   # dugme 2 s piše „Vrata otvorena“ (i drugim korisnicima)
                self.mark()
                asyncio.get_running_loop().call_later(2.2, self.mark)
            return {'err': r['err']} if r.get('err') else {'ok': d['name'] + (': otvaram' if cmd == 'pulse' else ': ' + ('uključeno' if cmd == 'on' else 'isključeno'))}
        if c == 'dev_edit':
            drv = DEV_DRIVERS.get(d.get('proto'))
            if drv and drv.dtype == 'camera':   # kamera: korisnik/lozinka/kanal ili adresa, bez provere IPv4 za „Drugi“
                cam, host, err = self.cam_fields(a, drv, d)
                if err:
                    return {'err': err}
                f = {'ip': host or (str(a.get('ip')).strip() if a.get('ip') else d['ip']),
                     'name': str(a.get('name') or d['name']).strip()[:40], 'room': str(a.get('room') if a.get('room') is not None else d.get('room', '')).strip()[:30]}
                d.update(f, cam=cam)
                self.cctv_stop(d['id'])   # nova adresa/lozinka: strim se pokreće iznova
                save_state(self.st)
                await self.dev_poll(d)
                self.mark()
                return {'ok': 'Sačuvano: ' + d['name']}
            f, err = self.dev_fields(a, d)
            if err:
                return {'err': err}
            if f['ip'] != d['ip']:
                if any(x['ip'] == f['ip'] and x is not d for x in devs):
                    return {'err': 'Uređaj sa tom adresom već postoji.'}
                if drv and drv.ready and await drv.probe(f['ip']) is None:
                    return {'err': 'Na %s nema odgovora.' % f['ip']}
            d.update(f, name=f['name'] or d['name'])
            save_state(self.st)
            await self.dev_poll(d)
            self.mark()
            return {'ok': 'Sačuvano: ' + d['name']}
        if c == 'dev_del':
            self.cctv_stop(d['id'])
            devs.remove(d)
            getattr(self, 'dev_state', {}).pop(d['id'], None)
            save_state(self.st)
            self.mark()
            return {'ok': 'Uklonjeno: ' + d['name']}
        if c == 'dev_set' and d.get('type') == 'tv' and a.get('k'):   # TV: jačina, kanal, ulaz, YouTube
            err = await DEV_DRIVERS[d['proto']].remote(d, str(a['k']), a.get('v'))
            if err:
                return {'err': '%s: %s' % (d['name'], err)}
            await self.dev_poll(d)
            return {}
        if c == 'dev_set':
            drv = DEV_DRIVERS.get(d.get('proto'))
            if not drv or not drv.ready:
                return {'err': 'Proizvođač nije podržan.'}
            rgb = a.get('rgb') if re.fullmatch(r'#?[0-9a-fA-F]{6}', str(a.get('rgb') or '')) else None
            ok = await drv.set(d, on=a.get('on'), dim=a.get('dim'), temp=a.get('temp'), rgb=rgb, mode=a.get('mode'))
            if not ok:
                return {'err': '%s ne odgovara.' % d['name']}
            await self.dev_poll(d)
            return {}
    # ---------- CCTV: kamera -> HLS (ffmpeg samo prepakuje, bez pretvaranja slike), samo dok neko gleda
    def cctv_stop(self, dev_id=None, key=None):
        for k, s in list(getattr(self, 'cctv', {}).items()):
            if key == k or (key is None and (dev_id is None or k.startswith(dev_id + '-'))):
                try:
                    s['proc'].kill()
                except ProcessLookupError:
                    pass
                shutil.rmtree(s['dir'], ignore_errors=True)
                del self.cctv[k]
                log.info('CCTV %s ugašen', k)

    async def cctv_start(self, d, sub):
        if not hasattr(self, 'cctv'):
            self.cctv = {}
        drv = DEV_DRIVERS.get(d.get('proto'))
        if isinstance(drv, UsbCamDriver):   # USB kamera: jedan strim (uređaj može da otvori samo jedan program)
            sub = False
        key = '%s-%s' % (d['id'], 's' if sub else 'm')
        s = self.cctv.get(key)
        if s and s['proc'].returncode is None:
            s['t'] = time.monotonic()
            return s
        self.cctv_stop(key=key)
        usb = isinstance(drv, UsbCamDriver)
        url = drv.url(d, sub) if isinstance(drv, CameraDriver) else ''
        if not url and not usb:
            return None
        out = CCTV_DIR / key
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)
        hls = ['-f', 'hls', '-hls_time', '1', '-hls_list_size', '5', '-hls_flags', 'delete_segments+omit_endlist+independent_segments',
               '-hls_segment_type', 'fmp4', str(out / 'index.m3u8')]
        args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-fflags', 'nobuffer']
        if usb:   # JPEG sa kamere -> H.264 hardverskim enkoderom Pi-ja, ključna slika na ~1 s
            args += drv.ffmpeg_input(d) + ['-an', '-c:v', 'h264_v4l2m2m', '-b:v', '1200k', '-g', '8', '-pix_fmt', 'yuv420p'] + hls
        else:   # IP kamera: slika se samo prepakuje (bez pretvaranja)
            if url.startswith('rtsp'):
                args += ['-rtsp_transport', 'tcp', '-timeout', '5000000']
            args += ['-i', url, '-an', '-c:v', 'copy'] + hls
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        self.cctv[key] = s = {'proc': proc, 'dir': out, 't': time.monotonic()}
        log.info('CCTV %s pokrenut (%s)', key, 'sporedni' if sub else 'glavni')
        if not hasattr(self, 'cctv_reaper'):
            self.cctv_reaper = asyncio.get_running_loop().create_task(self.cctv_reap())
        return s

    # ---------- USB kamera kao MJPEG (kamera već daje JPEG): slike idu pravo u pregledač, kašnjenje < 0,5 s,
    # bez dekodiranja/kodiranja. Jedan ffmpeg po kameri (uređaj se otvara samo jednom), slike se dele svim gledaocima.
    async def mjpeg_reader(self, d):
        rd = self.__dict__.setdefault('mjpeg', {})
        r = rd.get(d['id'])
        if r and r['proc'].returncode is None:
            return r
        dev = (d.get('cam') or {}).get('dev', '')
        self.cctv_stop(d['id'])   # HLS strim iste kamere drži uređaj
        # mjpeg2jpeg: EyeToy šalje JPEG bez Huffman tabela (kao AVI MJPEG) — pregledač ih traži
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'v4l2', '-input_format', 'mjpeg', '-video_size', '640x480',
            '-framerate', '15', '-i', dev, '-c:v', 'copy', '-bsf:v', 'mjpeg2jpeg', '-flush_packets', '1', '-f', 'mjpeg', 'pipe:1',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        r = rd[d['id']] = {'proc': proc, 'frame': None, 'seq': 0, 'ev': asyncio.Event(), 'viewers': 0, 't': time.monotonic()}
        log.info('USB kamera %s: MJPEG pokrenut', d.get('name'))

        async def pump():
            buf = b''
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:   # celi JPEG-ovi: FFD8 ... FFD9
                    a = buf.find(b'\xff\xd8')
                    b = buf.find(b'\xff\xd9', a + 2) if a >= 0 else -1
                    if a < 0 or b < 0:
                        buf = b'' if a < 0 else buf[a:]   # bez početka slike: ostatak je otpad
                        break
                    r['frame'], buf = buf[a:b + 2], buf[b + 2:]
                    r['seq'] += 1
                    r['ev'].set()
                    r['ev'] = asyncio.Event()
            log.info('USB kamera %s: MJPEG završen', d.get('name'))

        async def reap():   # rezerva: niko se nije ni povezao (npr. otvaranje prekinuto) -> ugasi posle 10 s
            while proc.returncode is None:
                await asyncio.sleep(2)
                if r['viewers'] <= 0 and time.monotonic() - r['t'] > 10:
                    proc.kill()
            rd.pop(d['id'], None)
        loop = asyncio.get_running_loop()
        loop.create_task(pump())
        loop.create_task(reap())
        return r

    async def cctv_reap(self):
        """Gasi strimove koje niko ne gleda (stranica traži playlistu na ~1 s dok gleda)."""
        while True:
            await asyncio.sleep(5)
            for k, s in list(getattr(self, 'cctv', {}).items()):
                if time.monotonic() - s['t'] > CCTV_IDLE or s['proc'].returncode is not None:
                    self.cctv_stop(key=k)

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
        cfg = self.st.setdefault('smb', {'on': False, 'usb': True})
        if 'users' not in cfg:   # ranije jedan korisnik (user/pw/pass) -> spisak; lozinka se više ne čuva kod nas, samo u Sambi
            u = cfg.get('user')
            cfg['users'] = [u] if cfg.get('pw') and u else []
            cfg['sysusers'] = [u] if cfg['users'] and u not in (USER, 'wds') else []
            cfg.pop('pass', None)
        cfg.pop('guest', None)   # gost (bez lozinke) više ne postoji
        return cfg

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
        wds = self.wds_path()   # WDS (Alati): ISO sa flash-a deljen kao \\TECOPI\WDS, i kad je obično deljenje isključeno
        share_usb = cfg['on']
        auto = share_usb or self.wds_cfg().get('on') or time.time() - getattr(self, 'wds_peek', 0) < 300 \
            or time.time() - getattr(self, 'stor_peek', 0) < 300   # WDS traži ISO na flash-u; Skladište je otvoreno
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
        if 'shares' not in cfg:   # ranije: svaki USB disk sam po sebi deljen (imena i folderi po disku) -> spisak share-ova
            names, dirs, old = cfg.pop('names', {}) or {}, cfg.pop('dirs', {}) or {}, []
            for p in parts:
                rel = dirs.get(p['id'], '')
                n = names.get(p['id']) or (re.sub(r'[^A-Za-z0-9_-]', '', Path(rel).name.replace(' ', '_'))[:20] if rel else '') or p['n']
                old.append({'id': secrets.token_hex(4), 'n': n, 'disk': p['id'], 'dir': rel,
                            'game': (cfg.get('proto') or 'NT1') == 'NT1', 'label': p['label'] or p['n']})
            cfg['shares'] = old
            cfg.pop('usb', None), cfg.pop('proto', None), cfg.pop('sd', None)
            save_state(self.st)
        shares = []
        for d in cfg['shares']:   # {id, n, disk (UUID), dir (folder na disku, prazno = ceo disk), game (PS2/OPL: SMB1)}
            p = next((p for p in parts if p['id'] == d['disk']), None)
            if not (p and p['mnt'] and share_usb):
                continue
            path = Path(p['mnt']) / d['dir']
            if not path.is_dir():   # folder obrisan ili preimenovan na računaru
                continue
            shares.append({'n': d['n'], 'id': d['id'], 'path': str(path), 'dir': d['dir'], 'usb': True, 'game': d.get('game', False),
                           'dev': p['dev'], 'fs': p['fs'], 'label': p['label']})
        for s in shares:
            try:
                du = shutil.disk_usage(s['path'])
                s['size'], s['free'] = du.total, du.free
            except OSError:
                pass
            s['opl'] = os.path.isdir(os.path.join(s['path'], 'DVD'))
            s['games'] = self.share_games(s['path'])
        self.smb_shares = shares
        conf = self.smb_conf_text(shares, cfg, wds)
        want = cfg['on'] or bool(wds)
        if force or conf != getattr(self, 'smb_conf', None):
            try:
                same = SMB_CONF.read_text() == conf
            except OSError:
                same = False
            if not same:
                p = await asyncio.create_subprocess_exec('sudo', '-n', 'tee', str(SMB_CONF), stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await p.communicate(conf.encode())
                if not p.returncode and want and getattr(self, 'smb_run', False):
                    # NetBIOS (port 139) se uključuje/isključuje samo restartom
                    await self._rc('sudo', '-n', 'systemctl', 'restart' if ('smb ports = 445 139' in conf) != ('smb ports = 445 139' in (getattr(self, 'smb_conf', None) or '')) else 'reload', 'smbd', timeout=30)
            self.smb_conf = conf
        run = (await self._run('systemctl', 'is-active', 'smbd', timeout=5)).strip() == 'active'
        if want and not run:
            await self._rc('sudo', '-n', 'systemctl', 'enable', '--now', 'smbd', timeout=30)
        elif not want and (run or force):
            await self._rc('sudo', '-n', 'systemctl', 'disable', '--now', 'smbd', 'nmbd', timeout=30)
        nmb = (await self._run('systemctl', 'is-active', 'nmbd', timeout=5)).strip() == 'active'
        if wds and not nmb:   # WDS: Teco.Pi vidljiv u „Mreža“ na Windows-u (NetBIOS ime TECOPI)
            await self._rc('sudo', '-n', 'systemctl', 'start', 'nmbd', timeout=30)
        elif not wds and nmb:
            await self._rc('sudo', '-n', 'systemctl', 'stop', 'nmbd', timeout=30)
        self.smb_run = (await self._run('systemctl', 'is-active', 'smbd', timeout=5)).strip() == 'active'
        self.mark()

    @staticmethod
    def smb_conf_text(shares, cfg, wds=None):
        """Uvek sa lozinkom (bez gosta). SMB1 (i NTLMv1) samo dok postoji game share: PS2 Open PS2 Loader zna samo SMB1,
        a Samba protokol podešava za ceo server, ne po share-u."""
        game = any(s.get('game') for s in shares)
        users = ' '.join(cfg.get('users') or []) or 'nobody'
        g = ['# Teco.Pi: ovaj fajl pravi server.py (Ekstenzije > SMB share); ručne izmene se gube.',
             '[global]',
             '   workgroup = WORKGROUP', '   server string = Teco.Pi', '   netbios name = TECOPI',
             '   server role = standalone server', '   map to guest = Never', '   restrict anonymous = 2',
             '   server min protocol = ' + ('NT1' if game else 'SMB2_10'),
             '   ntlm auth = ' + ('ntlmv1-permitted' if game else 'ntlmv2-only'),
             *(('   disable netbios = no', '   smb ports = 445 139') if wds else ('   disable netbios = yes', '   smb ports = 445')),
             '   load printers = no', '   printing = bsd', '   printcap name = /dev/null', '   disable spoolss = yes',
             '   logging = systemd', '   log level = 1', '   strict sync = no', '   use sendfile = yes']
        for s in shares:
            g += ['', '[%s]' % s['n'], '   path = ' + s['path'], '   read only = no', '   guest ok = no', '   valid users = ' + users,
                  '   force user = ' + USER, '   create mask = 0664', '   directory mask = 0775']
        if wds:   # Windows instalacija (ISO, samo čitanje): korisnik wds (WinPE se prijavljuje njim) ili korisnici share-a
            g += ['', '[WDS]', '   comment = Windows instalacija (Teco.Pi)', '   path = ' + wds, '   read only = yes', '   guest ok = no',
                  '   valid users = wds ' + (' '.join(cfg.get('users') or [])), '   force user = ' + USER]
        return '\n'.join(g) + '\n'

    @staticmethod
    def smb_name_ok(name):
        return re.fullmatch(r'[A-Za-z0-9_-]{1,20}', name) and name.lower() not in ('global', 'homes', 'printers', 'ipc$', 'wds')

    async def smb_share_add(self, a):
        """Novi share: ime, disk (uređaj), folder na disku (prazno = ceo disk), game = PS2/OPL (uključuje SMB1)."""
        cfg = self.smb_cfg()
        name = str(a.get('n') or '').strip()
        if not self.smb_name_ok(name):
            return {'err': 'Ime: do 20 slova, brojeva, - ili _ (bez razmaka i naših slova).'}
        if any(d['n'].lower() == name.lower() for d in cfg['shares']):
            return {'err': 'Već postoji share ' + name + '.'}
        p = next((p for p in getattr(self, 'usb', []) if p['dev'] == a.get('dev') and p['mnt']), None)
        if not p:
            return {'err': 'Izaberi priključen disk.'}
        d = self.inside(Path(p['mnt']), a.get('dir'))
        if not d or not d.is_dir():
            return {'err': 'Folder ne postoji.'}
        rel = '' if d == Path(p['mnt']).resolve() else str(d.relative_to(Path(p['mnt']).resolve()))
        game = bool(a.get('game'))
        if game:   # OPL očekuje svoje foldere u korenu share-a
            for x in OPL_DIRS:
                try:
                    (d / x).mkdir(exist_ok=True)
                except OSError:
                    pass
        cfg['shares'].append({'id': secrets.token_hex(4), 'n': name, 'disk': p['id'], 'dir': rel, 'game': game, 'label': p['label'] or p['n']})
        cfg['on'] = True
        save_state(self.st)
        await self.smb_refresh(force=True)
        return {'ok': 'Napravljen share %s%s' % (name, ' (game share · SMB1)' if game else '')}

    async def smb_share_set(self, a):
        """Izmena share-a: ime, folder ili game."""
        cfg = self.smb_cfg()
        d = next((d for d in cfg['shares'] if d['id'] == a.get('id')), None)
        if not d:
            return {'err': 'Share nije pronađen.'}
        if 'n' in a:
            name = str(a['n'] or '').strip()
            if not self.smb_name_ok(name):
                return {'err': 'Ime: do 20 slova, brojeva, - ili _ (bez razmaka i naših slova).'}
            if any(o['n'].lower() == name.lower() and o is not d for o in cfg['shares']):
                return {'err': 'Već postoji share ' + name + '.'}
            d['n'] = name
        if 'dir' in a:
            p = next((p for p in getattr(self, 'usb', []) if p['id'] == d['disk'] and p['mnt']), None)
            x = self.inside(Path(p['mnt']), a['dir']) if p else None
            if not x or not x.is_dir():
                return {'err': 'Folder ne postoji (disk priključen?).'}
            d['dir'] = '' if x == Path(p['mnt']).resolve() else str(x.relative_to(Path(p['mnt']).resolve()))
        if 'game' in a:
            d['game'] = bool(a['game'])
        save_state(self.st)
        await self.smb_refresh(force=True)
        return {'ok': 'Share %s je sačuvan' % d['n']}

    async def smb_share_del(self, a):
        """Briše samo share (podešavanje), fajlovi na disku ostaju."""
        cfg = self.smb_cfg()
        d = next((d for d in cfg['shares'] if d['id'] == a.get('id')), None)
        if not d:
            return {'err': 'Share nije pronađen.'}
        cfg['shares'].remove(d)
        await self._rc('sudo', '-n', 'smbcontrol', 'smbd', 'close-share', d['n'], timeout=10)
        save_state(self.st)
        await self.smb_refresh(force=True)
        return {'ok': 'Share %s je uklonjen (fajlovi su ostali na disku)' % d['n']}

    def smb_snapshot(self):
        cfg = self.smb_cfg()
        live = {s['id']: s for s in getattr(self, 'smb_shares', [])}
        parts = {p['id']: p for p in getattr(self, 'usb', [])}
        defs = []
        for d in cfg.get('shares', []):
            s, p = live.get(d['id']), parts.get(d['disk'])
            defs.append({**(s or {}), 'id': d['id'], 'n': d['n'], 'dir': d['dir'], 'game': d.get('game', False), 'label': d.get('label', ''),
                         'dev': p['dev'] if p else None, 'off': '' if s else ('isključeno' if not cfg['on'] else 'disk nije priključen' if not p or not p['mnt'] else 'folder ne postoji')})
        return {'inst': SMB_BIN.exists(), 'installing': bool(getattr(self, 'smb_installing', False)),
                'err': getattr(self, 'smb_err', None), 'on': cfg['on'],
                'proto': 'SMB1 + SMB2/3' if any(s.get('game') for s in live.values()) else 'SMB2/3',
                'users': cfg.get('users') or [], 'run': bool(getattr(self, 'smb_run', False)),
                'direct': bool(self.st.get('net_direct')),
                'shares': defs,
                'disks': [{'dev': p['dev'], 'n': p['label'] or p['n'], 'fs': p['fs'], 'size': p['size'], 'mnt': bool(p['mnt'])}
                          for p in getattr(self, 'usb', [])]}

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

    # ---------- WDS (Alati): Windows instalacija preko mreže
    # ISO sa flash-a se montira (samo čitanje) i deli kao \\TECOPI\WDS (NetBIOS); računar bez sistema se butuje sa
    # mreže: ruter i dalje deli adrese, dnsmasq je samo „proxy DHCP“ + TFTP (iPXE) -> iPXE preko HTTP-a vuče wimboot i
    # boot.wim sa ISO-a -> WinPE se poveže na \\<ip>\WDS (korisnik wds) i pokrene setup.exe.
    WDS_MNT = Path('/media/teco-wds')
    WDS_SRV = Path('/srv/teco-wds')        # TFTP: iPXE i boot.ipxe (čita ga dnsmasq kao root/nobody)
    WDS_BOOT = DATA / 'wds'                # wimboot (preuzet sa github.com/ipxe/wimboot)
    WDS_IPXE = ('undionly.kpxe', 'snponly.efi', 'ipxe.efi')
    WIMBOOT_URL = 'https://github.com/ipxe/wimboot/releases/latest/download/wimboot'

    def wds_cfg(self):
        return self.st.setdefault('wds', {'on': False, 'iso': ''})

    def wds_path(self):
        """Mesto montiranog ISO-a (za Sambu), ili None."""
        w = getattr(self, 'wds_rt', None) or {}
        return str(self.WDS_MNT) if w.get('mounted') else None

    def wds_ready(self):
        return {'samba': SMB_BIN.exists(), 'ipxe': os.path.exists('/usr/lib/ipxe/undionly.kpxe'),
                'dnsmasq': bool(shutil.which('dnsmasq') or os.path.exists('/usr/sbin/dnsmasq')), 'wimboot': (self.WDS_BOOT / 'wimboot').exists()}

    def wds_lan(self):
        """(interfejs, IP, mreža) na kojoj je ruter — tu se odgovara na PXE."""
        gw, best = self.net.get('gw'), None
        for i in self.net.get('ifs', []):
            if i['n'] in ('lo', 'tun0'):
                continue
            for a in i.get('ip') or []:
                try:
                    itf = ipaddress.ip_interface(a)
                except ValueError:
                    continue
                if itf.version != 4:
                    continue
                cand = (i['n'], str(itf.ip), itf.network)
                if gw and ipaddress.ip_address(gw) in itf.network:
                    return cand
                best = best or cand
        return best

    def wds_isos(self):
        """Windows ISO (≥ 100 MB) iz Skladišta: folder ISOs (i podfolderi) na priključenim flash diskovima."""
        out = []
        for p in getattr(self, 'usb', []):
            if not p.get('mnt'):
                continue
            root = os.path.join(p['mnt'], self.STOR_DIRS['iso'])
            for d, subs, files in os.walk(root):
                subs[:] = [s for s in subs if not s.startswith(('.', '$'))]
                for f in files:
                    if f.lower().endswith('.iso'):
                        fp = os.path.join(d, f)
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            continue
                        if sz >= 100 * 2**20:
                            out.append({'path': fp, 'n': f, 'dir': os.path.relpath(d, p['mnt']),
                                        'size': sz, 'disk': p['label'] or p['n']})
        return sorted(out, key=lambda x: x['n'].lower())

    @staticmethod
    def wds_find(root, rel):
        """Putanja u ISO-u bez obzira na velika/mala slova (boot/BCD, Sources/boot.wim…)."""
        cur = root
        for part in [x for x in rel.split('/') if x]:
            if part in ('.', '..'):
                return None
            try:
                names = os.listdir(cur)
            except OSError:
                return None
            m = part if part in names else next((n for n in names if n.lower() == part.lower()), None)
            if m is None:
                return None
            cur = os.path.join(cur, m)
        return cur

    def wds_view(self):
        cfg, w = self.wds_cfg(), getattr(self, 'wds_rt', None) or {}
        lan = self.wds_lan()
        return {'on': bool(cfg.get('on')), 'iso': cfg.get('iso') or '', 'mounted': bool(w.get('mounted')), 'pxe': bool(w.get('proc') and w['proc'].returncode is None),
                'err': w.get('err'), 'win': w.get('win'), 'ready': self.wds_ready(), 'installing': bool(getattr(self, 'wds_installing', False)),
                'ip': lan[1] if lan else None, 'log': list(w.get('log') or [])[-14:], 'isos': getattr(self, 'wds_iso_list', [])}

    def wds_note(self, txt):
        w = self.wds_rt = getattr(self, 'wds_rt', None) or {}
        lg = w.setdefault('log', [])
        if lg and lg[-1]['m'] == txt:
            lg[-1]['t'] = time.time()
        else:
            lg.append({'t': time.time(), 'm': txt})
            del lg[:-40]
        self.mark()

    async def wds_list(self):
        self.wds_peek = time.time()   # alat otvoren: flash diskovi se montiraju (samo radi traženja ISO-a)
        await self.smb_refresh()
        self.wds_iso_list = await asyncio.get_running_loop().run_in_executor(None, self.wds_isos)
        self.mark()
        return {}

    async def wds_install(self):
        if getattr(self, 'wds_installing', False):
            return {'ok': 'Instalacija je već u toku…'}
        self.wds_installing = True
        self.mark()

        async def job():
            err = None
            try:
                pk = [x for x, ok in (('samba', SMB_BIN.exists()), ('ipxe', self.wds_ready()['ipxe']), ('dnsmasq-base', self.wds_ready()['dnsmasq'])) if not ok]
                if pk:
                    await self._rc('sudo', '-n', 'apt-get', 'update', timeout=600)
                    rc, out = await self._rc('sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', '--no-install-recommends', *pk, timeout=1800)
                    if rc:
                        err = 'Instalacija nije uspela: ' + (out.splitlines() or [''])[-1][:160]
                        log.warning('wds apt: %s', out[-2000:])
                    elif 'samba' in pk:   # paket sam pokreće smbd/nmbd: Teco.Pi ih pali kad treba
                        await self._rc('sudo', '-n', 'systemctl', 'disable', '--now', 'smbd', 'nmbd', 'samba-ad-dc', timeout=60)
                if not err and not self.wds_ready()['wimboot']:
                    self.WDS_BOOT.mkdir(parents=True, exist_ok=True)
                    tmp = self.WDS_BOOT / 'wimboot.tmp'
                    rc, out = await self._rc('curl', '-fsSL', '--max-time', '120', '-o', str(tmp), self.WIMBOOT_URL, timeout=150)
                    if rc or tmp.stat().st_size < 30000:   # wimboot je ~75 KB
                        err = 'wimboot nije preuzet: ' + (out[:120] or 'prazan fajl')
                    else:
                        os.replace(tmp, self.WDS_BOOT / 'wimboot')
            except OSError as e:
                err = str(e)
            finally:
                self.wds_installing = False
            if err:
                self.wds_note(err)
            else:
                self.wds_note('Paketi za WDS su instalirani')
            await self.smb_refresh(force=True)
            self.mark()
        asyncio.get_running_loop().create_task(job())
        return {'ok': 'Instaliram (Samba, iPXE, dnsmasq, wimboot) — par minuta…'}

    async def wds_start(self, iso=None):
        cfg = self.wds_cfg()
        iso = iso or cfg.get('iso')
        rd = self.wds_ready()
        if not all(rd.values()):
            return {'err': 'Prvo instaliraj potrebne pakete (dugme „Instaliraj“).'}
        if not iso or not os.path.isfile(iso) or not any(x['path'] == iso for x in self.wds_isos()):
            return {'err': 'ISO nije pronađen na flash disku.'}
        lan = self.wds_lan()
        if not lan:
            return {'err': 'Teco.Pi nije na mreži.'}
        await self.wds_stop(keep=True)
        w = self.wds_rt = getattr(self, 'wds_rt', None) or {}
        w['err'] = None
        await self._rc('sudo', '-n', 'mkdir', '-p', str(self.WDS_MNT), timeout=5)
        rc, out = await self._rc('sudo', '-n', 'mount', '-o', 'loop,ro', iso, str(self.WDS_MNT), timeout=60)
        if rc:
            w['err'] = 'ISO se ne otvara: ' + (out.splitlines() or [''])[-1][:120]
            return {'err': w['err']}
        root = str(self.WDS_MNT)
        if not (self.wds_find(root, 'sources/boot.wim') and self.wds_find(root, 'setup.exe')
                and (self.wds_find(root, 'sources/install.wim') or self.wds_find(root, 'sources/install.esd') or self.wds_find(root, 'sources/install.swm'))):
            await self._rc('sudo', '-n', 'umount', root, timeout=30)
            w['err'] = 'To nije Windows instalacioni ISO (nema sources/boot.wim i install.wim/esd).'
            return {'err': w['err']}
        w['mounted'] = True
        w['win'] = await asyncio.get_running_loop().run_in_executor(None, self.wds_win_name, root)
        cfg.update(on=True, iso=iso)
        save_state(self.st)
        # Samba korisnik wds/wds: WinPE (Windows Setup) ne sme kao gost
        if not cfg.get('user_ok'):
            try:
                pwd.getpwnam('wds')
            except KeyError:
                await self._rc('sudo', '-n', 'useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', 'wds', timeout=15)
            rc, _ = await self._rc('sudo', '-n', 'smbpasswd', '-s', '-a', 'wds', inp='wds\nwds\n', timeout=15)
            cfg['user_ok'] = not rc
            save_state(self.st)
        await self.smb_refresh(force=True)
        err = await self.wds_pxe(lan)
        self.wds_note('Deli se %s · \\\\TECOPI\\WDS · PXE %s' % (os.path.basename(iso), 'radi' if not err else 'ne radi'))
        self.mark()
        return {'err': err} if err else {'ok': 'WDS radi'}

    @staticmethod
    def wds_win_name(root):
        """Ime izdanja iz sources/idwbinfo.txt ili cversion.ini (gruba oznaka, npr. Windows 11 · amd64)."""
        info = Teco.wds_find(root, 'sources/idwbinfo.txt')
        arch = ''
        try:
            txt = open(info, encoding='utf-8', errors='replace').read() if info else ''
            m = re.search(r'BuildArch=(\w+)', txt)
            arch = m.group(1) if m else ''
            m = re.search(r'BuildBranch=(\S+)', txt)
            br = m.group(1) if m else ''
        except OSError:
            br = ''
        v = 'Windows'
        ver = Teco.wds_find(root, 'sources/cversion.ini')
        try:
            m = re.search(r'MinServer=(\d+)', open(ver, errors='replace').read()) if ver else None
            if m:
                b = int(m.group(1))
                v = 'Windows 11' if b >= 22000 else 'Windows 10' if b >= 10240 else 'Windows'
        except OSError:
            pass
        return ' · '.join(x for x in (v, arch, br) if x)

    async def wds_pxe(self, lan):
        """dnsmasq kao proxy DHCP + TFTP (ruter i dalje deli adrese)."""
        ifn, ip, netw = lan
        srv = self.WDS_SRV
        await self._rc('sudo', '-n', 'install', '-d', '-m', '755', '-o', USER, str(srv), str(srv / 'tftp'), timeout=10)
        for f in self.WDS_IPXE:
            src = Path('/usr/lib/ipxe') / f
            if src.exists():
                shutil.copyfile(src, srv / 'tftp' / f)
        efi = 'snponly.efi' if (srv / 'tftp' / 'snponly.efi').exists() else 'ipxe.efi'
        base = 'http://%s%s/wds' % (ip, '' if PORT == 80 else ':%d' % PORT)
        (srv / 'tftp' / 'boot.ipxe').write_text(
            '#!ipxe\n'
            'echo Teco.Pi WDS: Windows instalacija preko mreze\n'
            'set base %s\n'
            'iseq ${platform} efi && set bcd efi/microsoft/boot/bcd || set bcd boot/bcd\n'
            'kernel ${base}/wimboot gui || goto fail\n'
            'initrd ${base}/winpeshl.ini winpeshl.ini\n'
            'initrd ${base}/install.bat install.bat\n'
            'initrd ${base}/iso/${bcd} BCD || goto fail\n'
            'initrd ${base}/iso/boot/boot.sdi boot.sdi || goto fail\n'
            'initrd ${base}/iso/sources/boot.wim boot.wim || goto fail\n'
            'boot || goto fail\n'
            ':fail\n'
            'echo Greska pri ucitavanju sa Teco.Pi. Komandna linija iPXE-a:\n'
            'shell\n' % base)
        conf = ['# Teco.Pi WDS: pravi ga server.py', 'port=0', 'log-dhcp', 'log-facility=-', 'interface=' + ifn, 'bind-dynamic',
                'dhcp-range=%s,proxy,%s' % (netw.network_address, netw.netmask),
                'enable-tftp', 'tftp-root=' + str(srv / 'tftp'), 'tftp-max=50',
                'dhcp-match=set:ipxe,175',
                'pxe-prompt="Teco.Pi WDS",2',
                'pxe-service=tag:!ipxe,x86PC,"Teco.Pi WDS - Windows instalacija",undionly.kpxe',
                'pxe-service=tag:!ipxe,X86-64_EFI,"Teco.Pi WDS - Windows instalacija",' + efi,
                'pxe-service=tag:!ipxe,BC_EFI,"Teco.Pi WDS - Windows instalacija",' + efi,
                'dhcp-boot=tag:ipxe,boot.ipxe']
        (srv / 'dnsmasq.conf').write_text('\n'.join(conf) + '\n')
        w = self.wds_rt
        try:
            proc = await asyncio.create_subprocess_exec('sudo', '-n', 'dnsmasq', '-k', '-C', str(srv / 'dnsmasq.conf'), '--pid-file=',
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as e:
            return 'dnsmasq se ne pokreće: %s' % e
        w['proc'] = proc

        async def read():
            async for line in proc.stdout:
                t = line.decode(errors='replace').strip()
                m = re.search(r'PXE\(\S+\) (\S+) proxy', t)
                if m:
                    self.wds_note('Računar %s traži mrežno butovanje' % m.group(1).upper())
                m = re.search(r'sent (\S+) to (\S+)', t)
                if m:
                    self.wds_note('%s preuzeo %s' % (m.group(2), os.path.basename(m.group(1))))
                if 'failed' in t or 'Address already in use' in t or 'illegal' in t:
                    w['err'] = t.split(': ', 1)[-1][:160]
                    self.wds_note('PXE: ' + w['err'])
            await proc.wait()
            if w.get('proc') is proc and not w.get('stopping'):
                w['proc'] = None
                w['err'] = w.get('err') or 'PXE (dnsmasq) se ugasio'
                self.mark()
        asyncio.get_running_loop().create_task(read())
        await asyncio.sleep(1.5)
        return w.get('err') if proc.returncode is not None else None

    async def wds_stop(self, keep=False):
        w = self.wds_rt = getattr(self, 'wds_rt', None) or {}
        p = w.get('proc')
        if p and p.returncode is None:
            w['stopping'] = True
            await self._rc('sudo', '-n', 'pkill', '-f', 'dnsmasq -k -C ' + str(self.WDS_SRV / 'dnsmasq.conf'), timeout=10)
            try:
                await asyncio.wait_for(p.wait(), 5)
            except asyncio.TimeoutError:
                pass
            w['stopping'] = False
        w['proc'] = None
        if w.get('mounted') or os.path.ismount(str(self.WDS_MNT)):
            w['mounted'] = False
            await self.smb_refresh(force=True)   # prvo skini deljenje, pa ISO
            rc, _ = await self._rc('sudo', '-n', 'umount', str(self.WDS_MNT), timeout=30)
            if rc:
                await self._rc('sudo', '-n', 'umount', '-l', str(self.WDS_MNT), timeout=30)
        if not keep:
            self.wds_cfg()['on'] = False
            save_state(self.st)
            w['err'] = None
            self.wds_note('WDS zaustavljen')
        self.mark()
        return {} if keep else {'ok': 'WDS zaustavljen'}

    async def wds_loop(self):
        """Posle paljenja Teco.Pi-ja (ili kad se flash vrati) WDS se sam podigne; ako ISO nestane, gasi se deljenje."""
        await asyncio.sleep(25)   # da se mreža i USB montiraju
        while True:
            try:
                cfg, w = self.wds_cfg(), getattr(self, 'wds_rt', None) or {}
                if cfg.get('on'):
                    if w.get('mounted') and not os.path.isfile(cfg.get('iso') or ''):
                        await self.wds_stop(keep=True)
                        self.wds_rt['err'] = 'Flash disk sa ISO-om je izvađen'
                        self.mark()
                    elif not w.get('mounted') and os.path.isfile(cfg.get('iso') or ''):
                        await self.wds_start()
                    elif w.get('mounted') and not (w.get('proc') and w['proc'].returncode is None):
                        lan = self.wds_lan()
                        if lan:
                            await self.wds_pxe(lan)
            except Exception:
                log.exception('wds')
            await asyncio.sleep(20)

    # ---------- Logički analizator: PICkit 2 kao izvor (pickit2.py, USB HID)
    LA_PK2_FILE = DATA / 'logic-pk2.json'

    def la_pk2_view(self):
        s = getattr(self, 'la_pk2', None) or {}
        return {'busy': bool(s.get('busy')), 'err': s.get('err'), 't': s.get('t', 0), 'conn': 'pickit2' in self.prog_usb()['progs']}

    async def la_pk2_capture(self):
        import threading
        import pickit2
        s = self.la_pk2 = getattr(self, 'la_pk2', None) or {}
        if s.get('busy'):
            return {'err': 'Snimanje je već u toku.'}
        p = self.prog_rt()
        if p['busy'] and p['tool'] in ('pic', 'avr'):
            return {'err': 'PICkit 2 je zauzet programiranjem.'}
        cfg = self.tool_cfg('logic')['pk2']
        self.la_pk2_cancel = ev = threading.Event()
        s.update(busy=True, err=None)
        self.mark()

        def job():
            dev = pickit2.PK2()
            try:
                return dev.capture(cfg['rate'], cfg['trig_ch'], cfg['trig'], cfg['win'], timeout=30, cancel=ev)
            finally:
                dev.close()
        try:
            cap = await asyncio.get_running_loop().run_in_executor(None, job)
            tmp = self.LA_PK2_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(cap))
            os.replace(tmp, self.LA_PK2_FILE)
            s.update(busy=False, err=None, t=cap['t'])
            log.info('PICkit 2: snimak %s, okidanje %s %s, odgovor %s', cap['rate'], cap['trig_ch'], cap['trig'], cap['resp'])
            if self.tool_running() == 'logic':
                self.tool_write('logic')   # TV: logic.py vidi nov snimak
                os.utime(self.LA_PK2_FILE)
            self.mark()
            return {'ok': 'Snimljeno 1024 uzorka po kanalu'}
        except (pickit2.PK2Error, OSError) as e:
            s.update(busy=False, err=str(e))
            self.mark()
            return {'err': str(e)}

    # ---------- Programatori (Alati → Elektronika): ESP32/ESP8266 (esptool) i AVR (avrdude)
    # Firmware: .bin/.hex sa flash-a ili poslat sa stranice (data/fw). Jedan posao u isto vreme; izlaz alata ide u log.
    FW_DIR = DATA / 'fw'
    ESPTOOL_VENV = DATA / 'esptool-venv'
    AVR_MCU = {'m328p': 'ATmega328P', 'm168': 'ATmega168', 'm8': 'ATmega8', 'm2560': 'ATmega2560', 'm32u4': 'ATmega32U4',
               't85': 'ATtiny85', 't45': 'ATtiny45', 't13': 'ATtiny13', 't2313': 'ATtiny2313'}
    AVR_PROG = {'arduino': 'Arduino bootloader (USB)', 'usbasp': 'USBasp', 'pickit2': 'PICkit 2 (ISP)', 'avrisp': 'Arduino kao ISP'}
    AVR_USB = ('usbasp', 'pickit2')   # USB programatori: bez porta, avrdude ih nalazi sam
    PK2CMD, PK2DIR = '/usr/local/bin/pk2cmd', '/usr/share/pk2/'   # PIC: Microchip pk2cmd (preveden na Pi-ju) + PK2DeviceFile.dat

    def prog_rt(self):
        if not hasattr(self, 'prog'):
            self.prog = {'busy': False, 'tool': '', 'what': '', 'log': [], 'pct': None, 'ok': None, 'proc': None, 'installing': False, 't': 0}
        return self.prog

    USB_PROGS = {('04d8', '0033'): 'pickit2', ('16c0', '05dc'): 'usbasp', ('04d8', '9012'): 'pickit4', ('03eb', '2104'): 'avrispmk2'}

    def prog_usb(self):
        """Priključeni programatori (USB VID:PID iz sysfs-a) i USB-serijski adapteri — za oznaku „priključen“ na stranici."""
        import serterm
        c = getattr(self, '_prog_usb_c', None)
        if c and time.time() - c[0] < 2:   # poziva se na svaki snimak stanja; sysfs je dovoljno čitati na 2 s
            return c[1]
        found = set()
        for d in glob.glob('/sys/bus/usb/devices/*/idVendor'):
            try:
                v = Path(d).read_text().strip()
                p = (Path(d).parent / 'idProduct').read_text().strip()
            except OSError:
                continue
            if (v, p) in self.USB_PROGS:
                found.add(self.USB_PROGS[(v, p)])
        r = {'progs': sorted(found), 'serial': [x['dev'] for x in serterm.ports()]}
        self._prog_usb_c = (time.time(), r)
        return r

    def esptool_cmd(self):
        py = self.ESPTOOL_VENV / 'bin' / 'python'
        if py.exists():
            return [str(py), '-m', 'esptool']
        for n in ('esptool', 'esptool.py'):
            p = shutil.which(n)
            if p:
                return [p]
        return None

    def prog_view(self):
        p = self.prog_rt()
        return {'busy': p['busy'], 'tool': p['tool'], 'what': p['what'], 'pct': p['pct'], 'ok': p['ok'], 'log': p['log'][-80:], 't': p['t'],
                'offer': {k: v for k, v in (p.get('offer') or {}).items() if k != 'path'} or None,
                'installing': p['installing'], 'esp': bool(self.esptool_cmd()), 'avr': bool(shutil.which('avrdude')), 'pic': os.path.exists(self.PK2CMD),
                'usb': self.prog_usb(), 'mode': getattr(self, 'prog_tvmode', ''),
                'mcu': self.AVR_MCU, 'progs': self.AVR_PROG}

    def prog_scan(self):
        """Firmware galerija: Firmware/<ESP|AVR|PIC>/<čip>/ na flash diskovima (oznaka = folder) + poslato sa stranice (data/fw)
        i fajlovi direktno u Firmware/ (bez oznake)."""
        out = []
        try:
            for f in sorted(self.FW_DIR.iterdir(), key=lambda x: -x.stat().st_mtime):
                if f.suffix.lower() in ('.bin', '.hex'):
                    out.append({'path': str(f), 'n': f.name, 'src': 'poslato', 'size': f.stat().st_size, 'fam': '', 'chip': ''})
        except OSError:
            pass
        for p in getattr(self, 'usb', []):
            if not p.get('mnt'):
                continue
            root = os.path.join(p['mnt'], self.STOR_DIRS['fw'])
            for d, subs, files in os.walk(root):
                subs[:] = [s for s in subs if not s.startswith('.')]
                rel = Path(os.path.relpath(d, root)).parts if d != root else ()
                fam = rel[0].upper() if rel and rel[0].upper() in self.FW_FAMS else ''
                chip = rel[1] if fam and len(rel) > 1 else ''
                for f in files:
                    if f.lower().endswith(('.bin', '.hex')) and not f.startswith('.'):
                        fp = os.path.join(d, f)
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            continue
                        if sz <= 16 * 2**20:
                            out.append({'path': fp, 'n': f, 'src': (p['label'] or p['n']) + '/Firmware' + ('/' + '/'.join(rel) if rel else ''),
                                        'size': sz, 'fam': fam, 'chip': chip})
                if len(out) > 1000:
                    break
        return out

    async def prog_list(self):
        import serterm
        self.wds_peek = time.time()   # flash diskovi se montiraju (samo za čitanje fajlova)
        await self.smb_refresh()
        self.prog_files = await asyncio.get_running_loop().run_in_executor(None, self.prog_scan)
        return {'files': [{'i': i, 'n': x['n'], 'src': x['src'], 'size': x['size'], 'fam': x['fam'], 'chip': x['chip']} for i, x in enumerate(self.prog_files)],
                'ports': serterm.ports()}

    def prog_log(self, line):
        p = self.prog_rt()
        line = line.rstrip()
        if not line:
            return
        m = re.search(r'\((\d{1,3}) ?%\)|\|\s*(\d{1,3})%|(\d{1,3}) ?%', line)
        if m:
            p['pct'] = int(next(g for g in m.groups() if g))
            if p['log'] and re.search(r'%', p['log'][-1]) and re.sub(r'[\d.%#\s|()]+', '', p['log'][-1])[:20] == re.sub(r'[\d.%#\s|()]+', '', line)[:20]:
                p['log'][-1] = line   # napredak u istom redu (Writing at …  (35 %))
                return
        p['log'].append(line)
        del p['log'][:-400]

    async def prog_run(self, tool, what, cmd, dev=None, save=None):
        """save: {path, fam, chip} — posle uspešnog upisa ponudi čuvanje u galeriju (Skladište → Firmware) ako ga tamo nema."""
        p = self.prog_rt()
        if p['busy']:
            return {'err': 'Već radi: ' + p['what']}
        if dev and getattr(self, 'term', None) and self.term.ser is not None and self.term.cfg.get('dev') == dev:
            self.term.close()   # serijski terminal je držao isti port
            self.term._status_all()
        p.update(busy=True, tool=tool, what=what, log=['$ ' + ' '.join(cmd)], pct=None, ok=None, t=time.time(), offer=None)
        self.mark()
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as e:
            p.update(busy=False, ok=False)
            p['log'].append(str(e))
            self.mark()
            return {'err': str(e)}
        p['proc'] = proc

        async def read():
            buf, last = b'', 0
            while True:
                ch = await proc.stdout.read(256)
                if not ch:
                    break
                buf += ch
                parts = re.split(rb'[\r\n]', buf)
                buf = parts.pop()
                for ln in parts:
                    self.prog_log(ln.decode(errors='replace'))
                if time.monotonic() - last > 0.3:
                    last = time.monotonic()
                    self.mark()
                    if self.tool_running() == 'prog':
                        self.tool_write('prog')   # TV: napredak uživo
            if buf:
                self.prog_log(buf.decode(errors='replace'))
            rc = await proc.wait()
            p.update(busy=False, ok=rc == 0, proc=None)
            if rc == 0 and p['pct'] is not None:
                p['pct'] = 100
            p['log'].append('✔ Gotovo.' if rc == 0 else '✖ Greška (kod %d).' % rc)
            if rc == 0 and save:
                p['offer'] = await asyncio.get_running_loop().run_in_executor(None, self.fw_offer, save, list(p['log']))
            log.info('programator %s %s: %s', tool, what, 'ok' if rc == 0 else rc)
            if self.tool_running() == 'prog':
                self.tool_write('prog')
            self.mark()
        asyncio.get_running_loop().create_task(read())
        return {'ok': what + '…'}

    def fw_offer(self, save, lines):
        """Upisani firmware: ponuda za galeriju samo ako na diskovima već nema istog fajla (isti sadržaj) za tu porodicu."""
        import hashlib
        disk = next((p for p in getattr(self, 'usb', []) if p.get('mnt')), None)
        if not disk:
            return None
        fam, chip = save['fam'], save['chip']
        if fam == 'ESP' and not chip:   # esptool: „Chip is ESP32-D0WD…“ / „Chip type: ESP32-S3…“
            m = re.search(r'Chip (?:is|type:)\s+(ESP\w+(?:-[SC]\d)?)', '\n'.join(lines))
            chip = m.group(1) if m else 'ESP32'
        try:
            h = hashlib.sha1(Path(save['path']).read_bytes()).hexdigest()
        except OSError:
            return None
        for p in getattr(self, 'usb', []):
            root = Path(p['mnt']) / self.STOR_DIRS['fw'] / fam if p.get('mnt') else None
            for x in (root.rglob('*') if root and root.is_dir() else []):
                try:
                    if x.is_file() and x.stat().st_size < 16 * 2**20 and hashlib.sha1(x.read_bytes()).hexdigest() == h:
                        return None   # već je u galeriji
                except OSError:
                    pass
        return {'path': save['path'], 'n': os.path.basename(save['path']), 'fam': fam, 'chip': chip, 'disk': disk['label'] or disk['n']}

    async def prog_save(self, a):
        """Ponuda posle upisa: v = sačuvaj (u Firmware/<porodica>/<čip>/ na prvom flash disku) ili odbij."""
        p = self.prog_rt()
        o, p['offer'] = p.get('offer'), None
        self.mark()
        if not a.get('v') or not o:
            return {}
        disk = next((x for x in getattr(self, 'usb', []) if x.get('mnt')), None)
        if not disk:
            return {'err': 'Nema priključenog flash diska.'}
        chip = re.sub(r'[\\/:*?"<>|]', '', str(a.get('chip') or o['chip'])).strip() or o['chip']
        d = Path(disk['mnt']) / self.STOR_DIRS['fw'] / o['fam'] / chip
        try:
            d.mkdir(parents=True, exist_ok=True)
            dest = self.free_name(d / o['n'])
            shutil.copyfile(o['path'], dest)
        except OSError as e:
            return {'err': 'Nije sačuvano: %s' % e.strerror}
        return {'ok': 'Sačuvano u galeriju: Firmware/%s/%s/%s' % (o['fam'], chip, dest.name)}

    def prog_file(self, i):
        try:
            return (getattr(self, 'prog_files', None) or [])[int(i)]['path']
        except (TypeError, ValueError, IndexError):
            return None

    async def prog_cmd(self, c, a):
        import serterm
        p = self.prog_rt()
        if c == 'prog_list':
            return await self.prog_list()
        if c == 'prog_stop':
            if p.get('proc') and p['proc'].returncode is None:
                p['proc'].terminate()
                return {'ok': 'Prekinuto'}
            return {}
        if c == 'prog_install':
            return await self.prog_install()
        if c == 'prog_save':
            return await self.prog_save(a)
        dev = str(a.get('dev') or '')
        if c == 'prog_esp':
            cmd0 = self.esptool_cmd()
            if not cmd0:
                return {'err': 'esptool nije instaliran (dugme „Instaliraj“).'}
            if dev not in [x['dev'] for x in serterm.ports()]:
                return {'err': 'Izaberi USB-serijski port.'}
            baud = str(int(a.get('baud') or 460800))
            base = cmd0 + ['--port', dev, '--baud', baud]
            new = '-m' in cmd0   # esptool 5 (venv): komande sa crticom; stari iz apt-a: sa donjom crtom
            sub = lambda s: s.replace('_', '-') if new else s
            act = a.get('act')
            if act == 'id':
                return await self.prog_run('esp', 'Prepoznavanje čipa', base + [sub('flash_id')], dev)
            if act == 'erase':
                return await self.prog_run('esp', 'Brisanje flash-a', base + [sub('erase_flash')], dev)
            if act == 'flash':
                f = self.prog_file(a.get('file'))
                if not f or not f.lower().endswith('.bin'):
                    return {'err': 'Izaberi .bin fajl.'}
                off = str(a.get('offset') or '0x0').strip().lower()
                if not re.fullmatch(r'0x[0-9a-f]{1,8}|\d{1,9}', off):
                    return {'err': 'Adresa: npr. 0x0 ili 0x10000.'}
                return await self.prog_run('esp', 'Upis ' + os.path.basename(f), base + [sub('write_flash'), '-z', off, f], dev, save={'path': f, 'fam': 'ESP', 'chip': ''})
            return {'err': 'Nepoznata radnja.'}
        if c == 'prog_pic':   # PICkit 2 + pk2cmd: PIC10/12/16/18 (i dsPIC/PIC24/PIC32 iz liste uređaja)
            if not os.path.exists(self.PK2CMD):
                return {'err': 'pk2cmd nije instaliran.'}
            part = re.sub(r'[^A-Za-z0-9]', '', str(a.get('part') or '')).upper()
            base = ['sudo', '-n', self.PK2CMD, '-B' + self.PK2DIR]   # USB (HID) uređaj: root
            pw = (['-W'] if a.get('ext') else []) + (['-T'] if a.get('hold') else []) + (['-R'] if a.get('run') else [])
            act = a.get('act')
            if act == 'id':
                return await self.prog_run('pic', 'Prepoznavanje čipa', base + ['-P'] + (['-W'] if a.get('ext') else []))
            if not part:
                return {'err': 'Upiši čip (npr. PIC16F628A) ili prvo „Prepoznaj čip“.'}
            pp = base + ['-P' + part]
            if act == 'blank':
                return await self.prog_run('pic', 'Provera da li je prazan', pp + ['-C'] + pw)
            if act == 'erase':
                return await self.prog_run('pic', 'Brisanje čipa', pp + ['-E'] + pw)
            if act == 'flash':
                f = self.prog_file(a.get('file'))
                if not f or not f.lower().endswith('.hex'):
                    return {'err': 'Izaberi .hex fajl.'}
                return await self.prog_run('pic', 'Upis ' + os.path.basename(f), pp + ['-F' + f, '-M', '-Y'] + pw, save={'path': f, 'fam': 'PIC', 'chip': part})
            if act == 'read':
                self.FW_DIR.mkdir(parents=True, exist_ok=True)
                out = self.FW_DIR / ('procitano_%s_%s.hex' % (part, time.strftime('%Y%m%d_%H%M%S')))
                return await self.prog_run('pic', 'Čitanje u ' + out.name, pp + ['-GF' + str(out)] + pw)
            return {'err': 'Nepoznata radnja.'}
        if c == 'prog_avr':
            if not shutil.which('avrdude'):
                return {'err': 'avrdude nije instaliran (dugme „Instaliraj“).'}
            mcu, prg = a.get('mcu'), a.get('prog')
            if mcu not in self.AVR_MCU or prg not in self.AVR_PROG:
                return {'err': 'Izaberi čip i programator.'}
            cmd = ['avrdude', '-p', mcu]
            if prg in self.AVR_USB:
                cmd = ['sudo', '-n'] + cmd + ['-c', prg]   # USB uređaj: bez udev pravila samo root
            else:
                if dev not in [x['dev'] for x in serterm.ports()]:
                    return {'err': 'Izaberi USB-serijski port.'}
                if prg == 'arduino':
                    cmd += ['-c', {'m2560': 'wiring', 'm32u4': 'avr109'}.get(mcu, 'arduino'), '-P', dev, '-b', str(int(a.get('baud') or 115200)), '-D']
                else:
                    cmd += ['-c', 'stk500v1', '-P', dev, '-b', '19200']
            if a.get('slow') and prg != 'arduino':   # nov čip iz fabrike radi na 1 MHz: sporiji ISP takt
                cmd += ['-B', '125kHz']
            act = a.get('act')
            if act == 'sig':
                return await self.prog_run('avr', 'Provera veze', cmd + ['-n'], dev)
            if act == 'fuse':
                return await self.prog_run('avr', 'Čitanje fuse bitova', cmd + ['-n', '-U', 'lfuse:r:-:h', '-U', 'hfuse:r:-:h', '-U', 'efuse:r:-:h'], dev)
            if act == 'erase':
                if prg == 'arduino':
                    return {'err': 'Preko bootloader-a se ne briše ceo čip (treba ISP).'}
                return await self.prog_run('avr', 'Brisanje čipa', cmd + ['-e'], dev)
            if act == 'flash':
                f = self.prog_file(a.get('file'))
                if not f:
                    return {'err': 'Izaberi .hex fajl.'}
                fmt = 'i' if f.lower().endswith('.hex') else 'r'
                return await self.prog_run('avr', 'Upis ' + os.path.basename(f), cmd + ['-U', 'flash:w:%s:%s' % (f, fmt)], dev, save={'path': f, 'fam': 'AVR', 'chip': self.AVR_MCU[mcu]})
            return {'err': 'Nepoznata radnja.'}
        return {'err': 'Nepoznata komanda.'}

    async def prog_install(self):
        p = self.prog_rt()
        if p['installing']:
            return {'ok': 'Instalacija je već u toku…'}
        p['installing'] = True
        self.mark()

        async def job():
            msgs = []
            try:
                venv_ok = self.ESPTOOL_VENV.exists() or any(Path('/usr/lib').glob('python3*/ensurepip'))
                pk = [x for x, ok in (('avrdude', shutil.which('avrdude')), ('python3-venv', venv_ok)) if not ok]
                if pk:
                    await self._rc('sudo', '-n', 'apt-get', 'update', timeout=600)
                    rc, out = await self._rc('sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', '--no-install-recommends', *pk, timeout=1800)
                    msgs.append('apt %s: %s' % (' '.join(pk), 'ok' if not rc else (out.splitlines() or [''])[-1][:120]))
                if not (self.ESPTOOL_VENV / 'bin' / 'python').exists():
                    rc, out = await self._rc('python3', '-m', 'venv', str(self.ESPTOOL_VENV), timeout=300)
                    if rc:
                        msgs.append('venv: ' + out[-160:])
                rc, out = await self._rc(str(self.ESPTOOL_VENV / 'bin' / 'pip'), 'install', '--upgrade', 'esptool', timeout=1200)
                msgs.append('esptool: ' + ('ok' if not rc else (out.splitlines() or [''])[-1][:160]))
            except OSError as e:
                msgs.append(str(e))
            finally:
                p['installing'] = False
            p['log'] = ['Instalacija: ' + ' · '.join(msgs)]
            self.mark()
        asyncio.get_running_loop().create_task(job())
        return {'ok': 'Instaliram esptool i avrdude — par minuta…'}

    # ---------- Pinout i datasheet (Alati → Elektronika): PDF-ovi sa flash-a
    def ds_scan(self):
        """PDF-ovi iz Skladišta: folderi Datasheets i Dokumenti na priključenim flash diskovima (najviše 3000)."""
        out = []
        for p in getattr(self, 'usb', []):
            if not p.get('mnt'):
                continue
            for root in (os.path.join(p['mnt'], self.STOR_DIRS['ds']), os.path.join(p['mnt'], self.STOR_DIRS['doc'])):
              for d, subs, files in os.walk(root):
                subs[:] = [s for s in subs if not s.startswith(('.', '$'))]
                for f in files:
                    if f.lower().endswith('.pdf') and not f.startswith('._'):
                        fp = os.path.join(d, f)
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            continue
                        out.append({'path': fp, 'n': f, 'dir': os.path.relpath(d, p['mnt']), 'size': sz, 'disk': p['label'] or p['n']})
                        if len(out) >= 3000:
                            return out
        return sorted(out, key=lambda x: x['n'].lower())

    async def ds_list(self):
        self.wds_peek = time.time()   # flash diskovi se montiraju (samo za čitanje fajlova), kao kod WDS-a
        await self.smb_refresh()
        self.ds_files = await asyncio.get_running_loop().run_in_executor(None, self.ds_scan)
        return {'files': [{'i': i, 'n': x['n'], 'dir': x['dir'], 'size': x['size'], 'disk': x['disk']} for i, x in enumerate(self.ds_files)],
                'usb': len([p for p in getattr(self, 'usb', []) if p.get('mnt')])}

    # ---------- DNS resolver (Alati → Network): Teco.Pi kao mali DNS server sa kešom (dnsmasq, poseban proces)
    # Uređaji (ili ruter u DHCP-u) kao DNS upišu IP Teco.Pi-ja. Lokalna imena: tecopi, wds (WDS server) + svoja.
    DNSR_SRV = Path('/srv/teco-dns')
    DNSR_UP = {'router': 'Ruter', '1.1.1.1': 'Cloudflare', '8.8.8.8': 'Google', '9.9.9.9': 'Quad9'}

    def dnsr_cfg(self):
        c = self.st.setdefault('dnsr', {})
        c.setdefault('on', False)
        c.setdefault('up', ['1.1.1.1', '8.8.8.8'])
        c.setdefault('cache', 1000)
        c.setdefault('hosts', [])   # [{'n': ime, 'ip': adresa}]
        c.setdefault('domain', 'lan')
        c.setdefault('vpn', True)   # odgovara i klijentima preko VPN-a (tun0)
        return c

    def dnsr_rt(self):
        if not hasattr(self, 'dnsr_st'):
            self.dnsr_st = {'proc': None, 'err': None, 'log': [], 'n': 0, 'cached': 0, 'fwd': 0, 'local': 0, 'blocked': 0,
                            'top': {}, 'clients': {}, 'since': 0, 'pend': {}}
        return self.dnsr_st

    def dnsr_view(self):
        c, r = self.dnsr_cfg(), self.dnsr_rt()
        lan = self.wds_lan()
        top = sorted(r['top'].items(), key=lambda x: -x[1])[:8]
        cl = sorted(r['clients'].items(), key=lambda x: -x[1])[:6]
        return {'on': c['on'], 'run': bool(r['proc'] and r['proc'].returncode is None), 'err': r['err'], 'ip': lan[1] if lan else None,
                'up': c['up'], 'ups': self.DNSR_UP, 'cache': c['cache'], 'domain': c['domain'], 'vpn': c['vpn'],
                'hosts': c['hosts'], 'auto': [{'n': n, 'ip': lan[1]} for n in ('tecopi', 'wds')] if lan else [],
                'n': r['n'], 'cached': r['cached'], 'fwd': r['fwd'], 'local': r['local'], 'since': r['since'],
                'top': top, 'clients': cl, 'log': r['log'][-40:], 'ready': bool(shutil.which('dnsmasq') or os.path.exists('/usr/sbin/dnsmasq'))}

    def dnsr_conf(self, lan):
        c = self.dnsr_cfg()
        ifn, ip, _ = lan
        ups = [(self.net.get('gw') if u == 'router' else u) for u in c['up']]
        g = ['# Teco.Pi DNS resolver: pravi ga server.py', 'port=53', 'bind-dynamic', 'interface=lo', 'interface=' + ifn,
             'no-resolv', 'no-hosts', 'domain-needed', 'bogus-priv', 'cache-size=%d' % c['cache'], 'log-queries', 'log-facility=-',
             'local=/%s/' % c['domain'], 'domain=%s' % c['domain']]
        if c['vpn'] and any(i['n'] == 'tun0' for i in self.net.get('ifs', [])):
            g.append('interface=tun0')
        g += ['server=' + u for u in ups if u]
        for n in ('tecopi', 'wds'):   # Teco.Pi i WDS (\\wds\WDS, http://tecopi)
            g.append('host-record=%s,%s.%s,%s' % (n, n, c['domain'], ip))
        for h in c['hosts']:
            g.append('host-record=%s,%s.%s,%s' % (h['n'], h['n'], c['domain'], h['ip']))
        return '\n'.join(g) + '\n'

    async def dnsr_start(self):
        r, c = self.dnsr_rt(), self.dnsr_cfg()
        await self.dnsr_stop(keep=True)
        lan = self.wds_lan()
        if not lan:
            r['err'] = 'Teco.Pi nije na mreži.'
            return r['err']
        await self._rc('sudo', '-n', 'install', '-d', '-m', '755', '-o', USER, str(self.DNSR_SRV), timeout=10)
        (self.DNSR_SRV / 'dnsmasq.conf').write_text(self.dnsr_conf(lan))
        try:
            proc = await asyncio.create_subprocess_exec('sudo', '-n', '/usr/sbin/dnsmasq', '-k', '-C', str(self.DNSR_SRV / 'dnsmasq.conf'), '--pid-file=',
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as e:
            r['err'] = 'dnsmasq se ne pokreće: %s' % e
            return r['err']
        r.update(proc=proc, err=None)
        if not r['since']:
            r['since'] = time.time()
        asyncio.get_running_loop().create_task(self._dnsr_read(proc))
        await asyncio.sleep(1)
        if proc.returncode is not None:
            r['err'] = r['err'] or 'DNS resolver se ugasio (port 53 zauzet?)'
        self.mark()
        return r['err']

    async def _dnsr_read(self, proc):
        """dnsmasq log-queries: upit, odgovor iz keša / sa servera / lokalno. Pamti poslednjih 200 i brojače."""
        r = self.dnsr_rt()
        last_mark = 0
        async for line in proc.stdout:
            t = line.decode(errors='replace').strip()
            m = re.search(r'(query)\[(\w+)\] (\S+) from (\S+)', t)
            if m:
                typ, name, cli = m.group(2), m.group(3), m.group(4)
                r['n'] += 1
                r['top'][name] = r['top'].get(name, 0) + 1
                r['clients'][cli] = r['clients'].get(cli, 0) + 1
                if len(r['top']) > 2000:
                    r['top'] = dict(sorted(r['top'].items(), key=lambda x: -x[1])[:500])
                r['log'].append({'t': time.time(), 'q': name, 'ty': typ, 'c': cli, 'a': '', 'k': ''})
                del r['log'][:-200]
            else:
                m = re.search(r'(cached|reply|config|forwarded) (\S+) (?:is|to) (\S+)', t)
                if m:
                    kind, name, ans = m.groups()
                    e = next((x for x in reversed(r['log'][-20:]) if x['q'] == name), None)
                    if kind == 'forwarded':
                        r['fwd'] += 1
                        if e:
                            e['k'] = 'fwd'
                    elif e and not e['a']:
                        e['a'] = ans
                        if kind == 'cached':
                            r['cached'] += 1
                            e['k'] = 'cache'
                        elif kind == 'config':
                            r['local'] += 1
                            e['k'] = 'local'
                        elif not e['k']:
                            e['k'] = 'fwd'
                elif 'failed' in t or 'Address already in use' in t or 'illegal' in t or 'bad ' in t:
                    r['err'] = t.split(': ', 1)[-1][:160]
            if time.monotonic() - last_mark > 1:   # stranici najviše jednom u sekundi
                last_mark = time.monotonic()
                self.mark()
        await proc.wait()
        if r.get('proc') is proc and not r.get('stopping'):
            r['proc'] = None
            r['err'] = r['err'] or 'DNS resolver se ugasio'
            self.mark()

    async def dnsr_stop(self, keep=False):
        r = self.dnsr_rt()
        p = r.get('proc')
        if p and p.returncode is None:
            r['stopping'] = True
            await self._rc('sudo', '-n', 'pkill', '-f', 'dnsmasq -k -C ' + str(self.DNSR_SRV / 'dnsmasq.conf'), timeout=10)
            try:
                await asyncio.wait_for(p.wait(), 5)
            except asyncio.TimeoutError:
                pass
            r['stopping'] = False
        r['proc'] = None
        if not keep:
            self.dnsr_cfg()['on'] = False
            save_state(self.st)
            r['err'] = None
        self.mark()

    async def dnsr_cmd(self, c, a):
        cfg = self.dnsr_cfg()
        if c == 'dnsr_on':
            cfg['on'] = bool(a.get('on'))
            save_state(self.st)
            if not cfg['on']:
                await self.dnsr_stop()
                return {'ok': 'DNS resolver isključen'}
            err = await self.dnsr_start()
            return {'err': err} if err else {'ok': 'DNS resolver radi'}
        if c == 'dnsr_set':
            up = [u for u in (a.get('up') or []) if u in self.DNSR_UP or re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', str(u))]
            if not up:
                return {'err': 'Izaberi bar jedan DNS server.'}
            cfg['up'] = up[:4]
            try:
                cfg['cache'] = max(150, min(10000, int(a.get('cache') or cfg['cache'])))
            except (TypeError, ValueError):
                return {'err': 'Keš je broj.'}
            cfg['vpn'] = bool(a.get('vpn', cfg['vpn']))
        elif c == 'dnsr_host':   # dodaj / ukloni lokalno ime (npr. nas -> 172.10.10.20)
            n = str(a.get('n') or '').strip().lower()
            if a.get('del'):
                cfg['hosts'] = [h for h in cfg['hosts'] if h['n'] != n]
            else:
                if not re.fullmatch(r'[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?', n) or n in ('tecopi', 'wds'):
                    return {'err': 'Ime: mala slova, brojevi i crtica (npr. nas, kamera-1).'}
                try:
                    ip = str(ipaddress.ip_address(str(a.get('ip') or '').strip()))
                except ValueError:
                    return {'err': 'Neispravna IP adresa.'}
                cfg['hosts'] = [h for h in cfg['hosts'] if h['n'] != n] + [{'n': n, 'ip': ip}]
                cfg['hosts'].sort(key=lambda h: h['n'])
        elif c == 'dnsr_clear':
            r = self.dnsr_rt()
            r.update(log=[], n=0, cached=0, fwd=0, local=0, top={}, clients={}, since=time.time())
            self.mark()
            return {}
        save_state(self.st)
        if cfg['on']:
            err = await self.dnsr_start()
            if err:
                return {'err': err}
        self.mark()
        return {'ok': 'Sačuvano'}

    async def dnsr_loop(self):
        """Posle paljenja Teco.Pi-ja resolver se sam podigne; ako se ugasi, ponovo za 20 s."""
        await asyncio.sleep(20)
        while True:
            try:
                r = self.dnsr_rt()
                if self.dnsr_cfg()['on'] and not (r['proc'] and r['proc'].returncode is None):
                    await self.dnsr_start()
            except Exception:
                log.exception('dns resolver')
            await asyncio.sleep(20)

    async def smb_user_add(self, user, pw):
        """Novi korisnik (ili nova lozinka postojećem). Windows 10/11 ne puštaju gosta; OPL može i sa korisnikom (npr. ps2).
        Svako ime dobija sistemski nalog bez prijave (samo za Sambu); svi korisnici čitaju, pišu i brišu,
        a fajlovi ostaju Teco.Pi-jevi (force user). Lozinku zna samo Samba (ne čuva se u stanju)."""
        cfg = self.smb_cfg()
        user, pw = str(user or '').strip(), str(pw or '')
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', user) or user in ('root', 'wds'):
            return {'err': 'Korisnik: mala slova, brojevi, - ili _ (npr. ps2).'}
        if not 4 <= len(pw) <= 64:
            return {'err': 'Lozinka treba da ima 4 do 64 znaka.'}
        try:
            pwd.getpwnam(user)
        except KeyError:
            rc, out = await self._rc('sudo', '-n', 'useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', user, timeout=15)
            if rc:
                return {'err': 'Korisnik nije napravljen: ' + out[:120]}
            cfg.setdefault('sysusers', []).append(user)
        rc, out = await self._rc('sudo', '-n', 'smbpasswd', '-s', '-a', user, inp='%s\n%s\n' % (pw, pw), timeout=15)
        if rc:
            return {'err': 'Lozinka nije sačuvana: ' + out[:120]}
        new = user not in cfg['users']
        if new:
            cfg['users'].append(user)
        save_state(self.st)
        await self.smb_refresh(force=True)
        return {'ok': ('Dodat korisnik %s' if new else 'Nova lozinka za %s') % user}

    async def smb_user_del(self, user):
        cfg = self.smb_cfg()
        if user not in cfg['users']:
            return {'err': 'Korisnik ne postoji.'}
        if len(cfg['users']) == 1 and cfg.get('shares'):
            return {'err': 'Poslednji korisnik: bez njega share-ovima niko ne može da pristupi. Prvo dodaj drugog.'}
        await self._rc('sudo', '-n', 'smbpasswd', '-x', user, timeout=15)
        if user in cfg.get('sysusers', []):   # nalog je napravio Teco.Pi samo za Sambu
            await self._rc('sudo', '-n', 'userdel', user, timeout=15)
            cfg['sysusers'].remove(user)
        cfg['users'].remove(user)
        save_state(self.st)
        await self.smb_refresh(force=True)
        return {'ok': 'Obrisan korisnik ' + user}

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

    # ---------- Skladište (Ekstenzije): sadržaj USB diskova po vrsti (igrice, ISO, video…), brisanje, slanje igrica
    STOR_KIND = {**{e: 'video' for e in ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.ts', '.mpg', '.mpeg', '.wmv')},
                 **{e: 'music' for e in ('.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.opus', '.wma')},
                 **{e: 'img' for e in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.bmp', '.tif', '.tiff')},
                 **{e: 'doc' for e in ('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.md', '.csv', '.odt', '.ods', '.rtf')},
                 **{e: 'iso' for e in ('.iso', '.img', '.wim', '.esd', '.vhd', '.vhdx', '.dmg')},
                 **{e: 'fw' for e in ('.hex', '.bin', '.elf', '.eep', '.uf2')}}
    STOR_SKIP = {'System Volume Information', '$RECYCLE.BIN', 'lost+found', '.Trashes', '.Spotlight-V100', '.fseventsd'}
    # gde se na disku čuva svaka vrsta (slanje iz Skladišta); igrice idu u OPL foldere (DVD/CD/POPS) game share-a
    STOR_DIRS = {'iso': 'ISOs', 'fw': 'Firmware', 'ds': 'Datasheets', 'video': 'Media/Filmovi', 'music': 'Media/Muzika', 'img': 'Media/Slike', 'doc': 'Dokumenti', 'data': 'Data'}
    STOR_NAMES = {'games': 'igrica', 'iso': 'ISO image', 'fw': 'firmware (.hex / .bin)', 'ds': 'datasheet (.pdf)', 'video': 'video', 'music': 'muzika', 'img': 'slika', 'doc': 'dokument', 'data': 'fajl'}
    FW_FAMS = ('ESP', 'AVR', 'PIC')   # Firmware/<porodica>/<čip>/fajl: oznaka za koji procesor je fajl (programatori prikazuju samo svoje)

    @staticmethod
    def free_name(dest):
        """Isto ime već postoji: „ime (2).ext“, „ime (3).ext“…"""
        if not dest.exists():
            return dest
        for i in range(2, 1000):
            d = dest.with_name('%s (%d)%s' % (dest.stem, i, dest.suffix))
            if not d.exists():
                return d
        return dest.with_name('%s (%s)%s' % (dest.stem, secrets.token_hex(3), dest.suffix))

    def stor_base(self, dev):
        """Gde idu igrice na disku: game share na tom disku (OPL ga vidi), inače koren diska."""
        sh = next((s for s in getattr(self, 'smb_shares', []) if s.get('dev') == dev and s.get('game')), None)
        if sh:
            return Path(sh['path'])
        p = next((p for p in getattr(self, 'usb', []) if p['dev'] == dev and p['mnt']), None)
        return Path(p['mnt']) if p else None

    def _stor_games(self, mnt, bases):
        """Igrice samo iz OPL foldera (DVD, CD, POPS) game share-a i korena diska; PS1 (.cue + .bin, .chd, .pbp) i u podfolderima CD-a."""
        out, seen = [], set()
        for base in bases:
            for top in ('DVD', 'CD', 'POPS'):
                root = Path(base) / top
                if not root.is_dir() or str(root.resolve()) in seen:
                    continue
                seen.add(str(root.resolve()))
                for cur, dirs, names in os.walk(root):
                    dirs[:] = [x for x in dirs if not x.startswith('.')]
                    cues = [n for n in names if n.lower().endswith('.cue')]
                    bins = sum(os.path.getsize(os.path.join(cur, n)) for n in names if n.lower().endswith('.bin')) if cues else 0
                    for n in names:
                        ext = os.path.splitext(n)[1].lower()
                        if n.startswith('.') or not (ext in GAME_EXT or ext in ('.cue', '.chd', '.pbp')):
                            continue
                        try:
                            size = os.path.getsize(os.path.join(cur, n))
                        except OSError:
                            continue
                        it = {'f': os.path.relpath(os.path.join(cur, n), mnt).replace(os.sep, '/'), 'size': size,
                              'kind': 'PS2' if ext in ('.iso', '.zso') and top != 'POPS' else 'PS1'}
                        if ext == '.cue':
                            it['size'] += bins // len(cues)
                        if ext == '.pbp' and cur != str(root):   # EBOOT.PBP: ime igre je ime foldera
                            it['name'] = os.path.basename(cur)
                        out.append(it)
        return sorted(out, key=lambda x: (x.get('name') or x['f'].rsplit('/', 1)[-1]).lower())

    def _stor_counts(self, mnt, bases):
        """Broj fajlova i zauzeće po vrsti: igrice + folderi Skladišta (ISOs, Media/…, Dokumenti, Data)."""
        games = self._stor_games(mnt, bases)
        cats = {'games': {'n': len(games), 'size': sum(g['size'] for g in games)}}
        for k, rel in self.STOR_DIRS.items():
            n = size = 0
            for cur, dirs, names in os.walk(os.path.join(mnt, rel)):
                dirs[:] = [x for x in dirs if not x.startswith('.')]
                for x in names:
                    if not x.startswith('.'):
                        try:
                            size += os.path.getsize(os.path.join(cur, x))
                            n += 1
                        except OSError:
                            pass
            cats[k] = {'n': n, 'size': size}
        return cats, games

    def stor_disk(self, dev):
        return next((p for p in getattr(self, 'usb', []) if p['dev'] == dev and p['mnt']), None)

    async def stor_scan(self, force=False, dev=None):
        """Diskovi + brojevi po vrsti za izabrani disk (prvi priključen ako nije izabran)."""
        self.stor_peek = time.time()   # dok je Skladište otvoreno, diskovi ostaju montirani
        if not any(p['mnt'] for p in getattr(self, 'usb', [])) or force:
            await self.smb_refresh()
        disks = []
        for p in getattr(self, 'usb', []):
            d = {'dev': p['dev'], 'n': p['label'] or p['n'], 'fs': p['fs'], 'size': p['size'], 'mnt': bool(p['mnt']),
                 'msg': getattr(self, 'usb_skip', {}).get(p['id'], '')}
            if p['mnt']:
                try:
                    du = shutil.disk_usage(p['mnt'])
                    d['size'], d['free'] = du.total, du.free
                except OSError:
                    pass
            disks.append(d)
        p = self.stor_disk(dev) or next((p for p in getattr(self, 'usb', []) if p['mnt']), None)
        if not p:
            return {'disks': disks, 'dev': None, 'cats': {}, 'dirs': self.STOR_DIRS}
        bases = [p['mnt']] + [s['path'] for s in getattr(self, 'smb_shares', []) if s.get('dev') == p['dev'] and s.get('game')]
        cats, games = await asyncio.get_running_loop().run_in_executor(None, self._stor_counts, p['mnt'], bases)
        self.stor_games = (p['dev'], games)
        return {'disks': disks, 'dev': p['dev'], 'cats': cats, 'dirs': self.STOR_DIRS}

    async def stor_ls(self, a):
        """Sadržaj foldera jedne vrste (kao u pregledaču fajlova); igrice = spisak igara iz OPL foldera."""
        p = self.stor_disk(a.get('dev'))
        cat = a.get('cat')
        if not p:
            return {'err': 'Disk nije priključen.'}
        if cat == 'games':
            g = getattr(self, 'stor_games', None)
            if not g or g[0] != p['dev']:
                bases = [p['mnt']] + [s['path'] for s in getattr(self, 'smb_shares', []) if s.get('dev') == p['dev'] and s.get('game')]
                g = (p['dev'], await asyncio.get_running_loop().run_in_executor(None, self._stor_games, p['mnt'], bases))
            return {'games': g[1][:1000]}
        if cat not in self.STOR_DIRS:
            return {'err': 'Nepoznata vrsta.'}
        root = Path(p['mnt']) / self.STOR_DIRS[cat]
        if cat == 'fw':   # galerija: folder po porodici procesora uvek postoji
            for fam in self.FW_FAMS:
                try:
                    (root / fam).mkdir(parents=True, exist_ok=True)
                except OSError:
                    break
        d = self.inside(root, a.get('path')) if root.is_dir() else None
        if root.is_dir() and (not d or not d.is_dir()):
            d = root.resolve()
        out = {'path': '' if not d or d == root.resolve() else str(d.relative_to(root.resolve())).replace(os.sep, '/'), 'dirs': [], 'files': [],
               'root': self.STOR_DIRS[cat]}
        if not d:
            return out   # folder još ne postoji: napravi se pri prvom slanju
        try:
            for e in sorted(os.scandir(d), key=lambda e: e.name.lower()):
                if e.name.startswith('.'):
                    continue
                if e.is_dir():
                    out['dirs'].append({'n': e.name})
                elif e.is_file():
                    out['files'].append({'n': e.name, 'size': e.stat().st_size, 't': int(e.stat().st_mtime)})
        except OSError as e:
            return {'err': 'Ne mogu da pročitam folder: %s' % e.strerror}
        out['files'] = out['files'][:2000]
        return out

    def stor_target(self, a):
        """Fajl ili folder unutar foldera Skladišta (vrsta + putanja) ili unutar OPL foldera (igrice: f od korena diska)."""
        p = self.stor_disk(a.get('dev'))
        if not p:
            return None, None
        if a.get('cat') == 'games':
            f = self.inside(Path(p['mnt']), a.get('f'))
            ok = f and f.is_file() and any(x in ('DVD', 'CD', 'POPS') for x in f.relative_to(Path(p['mnt']).resolve()).parts[:-1])
            return (f if ok else None), p
        if a.get('cat') not in self.STOR_DIRS:
            return None, p
        root = (Path(p['mnt']) / self.STOR_DIRS[a['cat']]).resolve()
        f = self.inside(root, a.get('f'))
        return (f if f and f != root and f.exists() else None), p

    async def stor_del(self, a):
        f, p = self.stor_target(a)
        if not f:
            return {'err': 'Fajl nije pronađen.'}
        try:
            if f.is_dir():
                await asyncio.get_running_loop().run_in_executor(None, shutil.rmtree, f)
            else:
                f.unlink()
                if f.suffix.lower() == '.cue':   # PS1: i .bin delovi iste igrice
                    for b in f.parent.glob('*.bin'):
                        b.unlink()
        except OSError as e:
            return {'err': 'Nije obrisano: %s' % e.strerror}
        await self.smb_refresh()
        return {'ok': 'Obrisano: ' + f.name}

    # ---------- Ekstenzije: glavni prekidač na vrhu svake (osim Skladišta; DNS resolver ima svoj)
    def ext_state(self):
        return {'pbx': bool(self.pbx_cfg().get('on')), 'sms': bool(self.phone_cfg()['gsm'].get('on')), 'mail': bool(self.mail_cfg().get('on')),
                'wds': bool(self.wds_cfg().get('on')), 'smb': bool(self.smb_cfg().get('on')), 'web': bool(self.web_cfg().get('on', True)),
                'bura': bool(self.bura_cfg().get('on')), 'cups': bool(self.cups_cfg().get('on')), 'ntp': bool(self.ntp_cfg().get('serve'))}

    # ---------- BURAscale (vaga preko USB COM-a): masa uživo na stranici (sopstvena WS poruka, ne celo stanje) i na TV-u
    BURA_SHM = Path('/dev/shm/teco-bura.json') if Path('/dev/shm').is_dir() else Path('/tmp/teco-bura.json')   # RAM: upis 20x u s, ne SD kartica

    def bura_cfg(self):
        return self.st.setdefault('bura', {'on': False, 'port': ''})

    def bura_view(self):
        b = getattr(self, 'bura', None)
        v = b.view() if b else {'online': False, 'disp': 'N/A', 'unit': ''}
        import bura
        return dict(v, on=bool(self.bura_cfg().get('on')), port=self.bura_cfg().get('port', ''), ports=bura.ports())

    def bura_changed(self):
        """Poziva ga bura.py na svaku promenu; šalje se najviše ~25x u sekundi (vaga je brza, a prikaz mora da prati)."""
        if getattr(self, '_bura_h', None):
            return
        last = getattr(self, '_bura_t', 0)
        self._bura_h = asyncio.get_running_loop().call_later(max(0, 0.04 - (time.time() - last)), lambda: asyncio.ensure_future(self.bura_flush()))

    async def bura_flush(self):
        self._bura_h, self._bura_t = None, time.time()
        v = self.bura_view()
        if self.tool_running() == 'bura':
            try:
                tmp = self.BURA_SHM.with_suffix('.tmp')
                tmp.write_text(json.dumps(v))
                os.replace(tmp, self.BURA_SHM)
            except OSError:
                pass
        await self.push({'bura': v})

    async def bura_loop(self):
        import bura
        self.bura = bura.Bura(self.bura_changed)
        while True:
            await asyncio.sleep(1)
            c, b = self.bura_cfg(), self.bura
            try:
                if not c.get('on'):
                    if b.ser:
                        b.close()
                        self.bura_changed()
                    continue
                if b.ser and time.time() - b.last_rx > 5:   # ništa ne stiže: ponovo otvori
                    b.err = 'vaga se ne javlja'
                    b.close()
                if not b.ser:
                    ps = bura.ports()
                    dev = c.get('port') or (ps[0] if ps else '')
                    if dev and dev in ps:
                        b.open(dev)
                    else:
                        b.err = 'vaga nije priključena (USB)'
                    self.bura_changed()
                elif time.time() - b.last_tx > 2:
                    b.send('PING')   # veza važi 5 s bez komandi
                b.ask()
                if b.ser and b.gaps and time.time() - getattr(self, '_bura_dbg', 0) > 15:   # dijagnostika: ritam redova mase
                    self._bura_dbg = time.time()
                    g = sorted(b.gaps)
                    log.info('BURAscale: %d redova, razmak sr %.0f ms, med %.0f, max %.0f ms; INF=%s CFG=%s ostalo=%s', len(g), 1000 * sum(g) / len(g),
                             1000 * g[len(g) // 2], 1000 * g[-1], b.raw_inf[:40], b.raw_cfg[:40], b.other[-1:])
                    import collections
                    log.info('BURAscale vrednosti (poslednjih %d): %s', len(getattr(b, 'raw', [])), dict(collections.Counter(getattr(b, 'raw', []))))
            except Exception:
                log.exception('BURAscale')

    async def bura_cmd(self, a):
        b = getattr(self, 'bura', None)
        k = a.get('k')
        if k == 'port':
            self.bura_cfg()['port'] = str(a.get('v') or '')
            save_state(self.st)
            if b:
                b.close()
            self.bura_changed()
            return {'ok': 'Port: ' + (self.bura_cfg()['port'] or 'automatski')}
        if not b or not b.online:
            return {'err': 'Vaga nije povezana.'}
        if k == 'pt':
            v = str(a.get('v') or '').strip().replace(',', '.')
            if not re.fullmatch(r'\d{1,4}(\.\d{1,4})?', v):
                return {'err': 'Ručna tara: upiši masu (npr. 0.250).'}
            cmd = 'PT ' + v
        else:
            cmd = {'zero': 'ZERO', 'tare': 'TARE', 'mplus': 'ALIBI SAVE %d' % int(time.time()), 'print': 'W'}.get(k)
            if not cmd:
                return {'err': 'Nepoznata komanda.'}
        b.reply = ''
        b.send(cmd)
        for _ in range(20):   # odgovor stiže za par desetina ms
            await asyncio.sleep(0.05)
            if b.reply:
                break
        r = b.reply
        if r.startswith(('E:', 'AL,E')):
            why = {'U': 'masa nije stabilna', 'R': 'van opsega', 'S': 'zagrevanje / startna nula', 'A': 'greška ADC', 'V': 'neispravna vrednost', 'D': 'nije deljivo sa e'}.get(r.rsplit(',', 1)[-1].strip(), r)
            return {'err': cmd.split()[0] + ': ' + why}
        return {'ok': {'zero': 'Nula', 'tare': 'Tara', 'mplus': 'Merenje sačuvano (alibi)', 'print': 'Štampa', 'pt': 'Ručna tara ' + str(a.get('v'))}[k] + (' · ' + r if r and not r.startswith('OK') else '')}

    # ---------- CUPS štampa: Pi deli mrežne štampače svim uređajima (AirPrint/IPP preko mDNS, Windows preko http://IP:631/printers/…)
    def cups_cfg(self):
        return self.st.setdefault('cups', {'on': False})

    async def cups_refresh(self):
        """Štampači, podrazumevani i poslovi iz lpstat (na 5 s dok je uključeno; ne u svakom stanju stranice)."""
        v = {'installed': bool(shutil.which('lpstat')), 'printers': [], 'jobs': [], 'default': ''}
        if v['installed'] and self.cups_cfg().get('on'):
            _, p = await self._rc('lpstat', '-l', '-p', timeout=8)
            _, dv = await self._rc('lpstat', '-v', timeout=8)
            _, d = await self._rc('lpstat', '-d', timeout=8)
            _, o = await self._rc('lpstat', '-o', timeout=8)
            uri = dict(re.findall(r'device for (\S+): (\S+)', dv or ''))
            cur, first = None, False
            for ln in (p or '').splitlines():
                m = re.match(r'printer (\S+) (?:is (\w+)|(disabled))', ln)
                if m:
                    cur, first = {'name': m.group(1), 'state': m.group(2) or 'disabled', 'desc': '', 'loc': '', 'uri': uri.get(m.group(1), ''), 'msg': ''}, True
                    v['printers'].append(cur)
                    continue
                if cur and first and ln.startswith('\t') and ':' not in ln and ln.strip():
                    cur['msg'] = ln.strip()   # poruka štampača (npr. nema papira) je red odmah ispod naziva
                first = False
                if cur and ln.strip().startswith('Description:'):
                    cur['desc'] = ln.split(':', 1)[1].strip()
                elif cur and ln.strip().startswith('Location:'):
                    cur['loc'] = ln.split(':', 1)[1].strip()
            m = re.search(r'destination: (\S+)', d or '')
            v['default'] = m.group(1) if m else ''
            for ln in (o or '').splitlines():
                m = re.match(r'(\S+)-(\d+)\s+(\S+)\s+(\d+)\s+(.*)$', ln)
                if m:
                    v['jobs'].append({'printer': m.group(1), 'id': int(m.group(2)), 'user': m.group(3), 'size': int(m.group(4)), 'when': m.group(5).strip()})
        self.cups_st = v
        self.mark()

    def cups_view(self):
        v = dict(getattr(self, 'cups_st', None) or {'installed': bool(shutil.which('lpstat')), 'printers': [], 'jobs': [], 'default': ''})
        lan = next(((i.get('ip') or [''])[0].split('/')[0] for i in self.net.get('ifs', []) if i.get('ip') and not i['n'].startswith('tun')), '')
        v.update(on=bool(self.cups_cfg().get('on')), ip=lan, found=getattr(self, 'cups_found', []), busy=getattr(self, 'cups_busy', ''))
        return v

    async def cups_loop(self):
        while True:
            try:
                if self.cups_cfg().get('on'):
                    await self.cups_refresh()
            except Exception:
                log.exception('CUPS')
            await asyncio.sleep(5)

    async def cups_cmd(self, a):
        k = a.get('k')
        name = re.sub(r'[^A-Za-z0-9_-]', '', str(a.get('name') or ''))[:40]
        if k == 'find':   # štampači u mreži (mDNS/IPP), bez Pi-jevih deljenih
            import socket
            _, out = await self._rc('ippfind', '-T', '4', timeout=15)
            mine = socket.gethostname().lower()
            have = {re.sub(r'^ipps?://([^/:]+).*', r'\1', p['uri']) for p in (getattr(self, 'cups_st', None) or {}).get('printers', [])}

            def known(u):   # već dodat (pod IP adresom ili imenom)
                host = re.sub(r'^ipps?://([^/:]+).*', r'\1', u)
                if host in have:
                    return True
                try:
                    return socket.gethostbyname(host) in have
                except OSError:
                    return False
            self.cups_found = sorted({u for u in (out or '').split() if u.startswith('ipp') and mine not in u.lower() and not known(u)})
            self.mark()
            return {'ok': 'Pronađeno novih: %d' % len(self.cups_found) if self.cups_found else 'Nema novih štampača u mreži (već dodati se ne nude; IP se može upisati ručno).'}
        if k == 'add':
            src = str(a.get('uri') or a.get('ip') or '').strip()
            if re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', src):
                cands = ['ipp://%s/ipp/print' % src, 'ipp://%s/ipp' % src]
            elif re.fullmatch(r'ipps?://[A-Za-z0-9.\-:]+/[\w/.\-]*', src):
                cands = [src]
            else:
                return {'err': 'Upiši IP adresu štampača (npr. 172.10.10.133).'}
            desc = str(a.get('desc') or '').strip()[:60]
            name = name or re.sub(r'[^A-Za-z0-9]', '', desc)[:30] or 'Stampac' + src.split('/')[2].split('.')[-1].split(':')[0]
            self.cups_busy = 'Dodajem %s…' % name
            self.mark()
            err = ''
            try:
                for uri in cands:   # bez drajvera (IPP Everywhere / AirPrint): CUPS sam pita štampač šta ume
                    rc, out = await self._rc('lpadmin', '-p', name, '-D', desc or name, '-E', '-v', uri, '-m', 'everywhere', '-o', 'printer-is-shared=true', timeout=60)
                    if not rc:
                        break
                    err = ((out or '').strip().splitlines()[-1:] or [''])[0]
                else:
                    # IPP radi, ali štampač prima samo svoj jezik: Samsung QPDL/SPL (Xerox Phaser 3020/3025, Samsung M20xx, ML-21xx…)
                    # -> SpliX drajver i direktan port 9100
                    _, attrs = await self._rc('ipptool', '-tv', cands[0], 'get-printer-attributes.test', timeout=20)
                    host = cands[0].split('/')[2]
                    if 'x-QPDL' in (attrs or '') and Path('/usr/share/cups/drv/splix-samsung.drv').exists():
                        rc, out = await self._rc('lpadmin', '-p', name, '-D', desc or name, '-E', '-v', 'socket://%s:9100' % host, '-m', 'drv:///splix-samsung.drv/ml2160.ppd',
                                                 '-o', 'printer-is-shared=true', '-o', 'PageSize=A4', timeout=60)
                        if rc:
                            return {'err': 'Drajver SpliX nije uspeo: ' + ((out or '').strip().splitlines()[-1:] or [''])[0][:120]}
                        # samo A4/A5: telefon sa US podešavanjem bi poslao Letter, a štampač bez ekrana tada ćuti i čeka taj papir
                        await self._rc('python3', str(BASE / 'system' / 'cups-a4.py'), name, timeout=20)
                        await self._rc('systemctl', 'restart', 'cups', timeout=30)
                    else:
                        return {'err': 'Štampač ne radi bez drajvera (%s).' % err[:120]}
                if not self.cups_st.get('default') if getattr(self, 'cups_st', None) else True:
                    await self._rc('lpadmin', '-d', name, timeout=15)
            finally:
                self.cups_busy = ''
            await self.cups_refresh()
            return {'ok': 'Dodat štampač ' + name + ' · deli se svim uređajima u mreži'}
        if not name:
            if k != 'cancel':
                return {'err': 'Izaberi štampač.'}
        if k == 'del':
            rc, out = await self._rc('lpadmin', '-x', name, timeout=20)
        elif k == 'default':
            rc, out = await self._rc('lpadmin', '-d', name, timeout=20)
        elif k == 'test':
            rc, out = await self._rc('lp', '-d', name, '-t', 'Teco.Pi probna stranica', '/usr/share/cups/data/default-testpage.pdf', timeout=30)
        elif k == 'resume':   # štampač se zaustavio posle greške: ponovo uključi
            rc, out = await self._rc('cupsenable', name, timeout=20)
        elif k == 'cancel':
            rc, out = await self._rc('cancel', str(int(a.get('id') or 0)), timeout=20)
        else:
            return {'err': 'Nepoznata komanda.'}
        await self.cups_refresh()
        if rc:
            return {'err': (out or 'Nije uspelo.').strip().splitlines()[-1][:160]}
        return {'ok': {'del': 'Uklonjen ' + name, 'default': 'Podrazumevani: ' + name, 'test': 'Probna stranica poslata na ' + name, 'resume': name + ' ponovo radi', 'cancel': 'Posao otkazan'}[k]}

    async def ext_on(self, ext, on):
        if ext == 'ntp':   # glavni prekidač = NTP server za druge uređaje (sam Pi uvek uzima vreme)
            self.ntp_cfg()['serve'] = on
            save_state(self.st)
            err = await self.ntp_write()
            if err:
                return {'err': err}
            self.mark()
            return {'ok': 'NTP server je ' + ('uključen · uređaji mogu da uzimaju vreme sa Teco.Pi-ja' if on else 'isključen (Pi i dalje uzima vreme)')}
        if ext == 'cups':
            if on and not shutil.which('lpstat'):
                return {'err': 'CUPS nije instaliran (pokreni system/cups.sh).'}
            self.cups_cfg()['on'] = on
            save_state(self.st)
            await self._rc('sudo', '-n', 'systemctl', 'enable' if on else 'disable', '--now', 'cups', timeout=60)
            await self.cups_refresh()
            return {'ok': 'Štampa (CUPS) je ' + ('uključena · štampači se dele svim uređajima' if on else 'isključena')}
        if ext == 'bura':
            self.bura_cfg()['on'] = on
            save_state(self.st)
            self.bura_changed()
            self.mark()
            return {'ok': 'BURAscale je ' + ('uključen · tražim vagu na USB-u' if on else 'isključen')}
        if ext == 'pbx':   # centrala: i Asterisk servis (ne troši ništa kad je isključena, ne kreće pri paljenju)
            r = self.pbx_set({'on': on})
            if isinstance(r, dict) and r.get('err'):
                return r
            await self._rc('sudo', '-n', 'systemctl', 'enable' if on else 'disable', '--now', 'asterisk', timeout=60)
            k = getattr(self, '_pbx_kick', None)
            if k:
                k.set()
            msg = 'VoIP / FXS je ' + ('uključen' if on else 'isključen')
        elif ext == 'sms':
            r = self.phone_set({'gsm': {'on': on}})
            if isinstance(r, dict) and r.get('err'):
                return r
            k = getattr(self, '_sms_kick', None)
            if k:
                k.set()
            msg = 'Modem (SMS) je ' + ('uključen' if on else 'isključen')
        elif ext == 'mail':
            m = self.mail_cfg()
            m['on'] = on
            save_state(self.st)
            msg = 'Email je ' + ('uključen' if on else 'isključen') + ('' if not on or (m['host'] and m['from'] and m['to']) else ' · upiši nalog i klikni Sačuvaj')
        elif ext == 'wds':
            if not on:
                return await self.wds_stop()
            cfg = self.wds_cfg()
            if cfg.get('iso') and os.path.isfile(cfg['iso']):
                r = await self.wds_start()
                if not r.get('err'):
                    return r
            cfg['on'] = True   # čeka izbor ISO-a (ili flash disk)
            save_state(self.st)
            msg = 'WDS je uključen · izaberi ISO ispod'
        elif ext == 'smb':
            r = await self.smb_set('smb', on)
            if r.get('err') and on and SMB_BIN.exists():
                self.mark()
                return {'ok': 'SMB je uključen · ' + r['err']}
            return r
        elif ext == 'web':
            self.web_cfg()['on'] = on
            save_state(self.st)
            msg = 'Web monitor ' + ('prati sajtove' if on else 'je pauziran')
        else:
            return {'err': 'Nepoznata ekstenzija.'}
        self.mark()
        return {'ok': msg}

    # ---------- Email (Ekstenzije): SMTP nalog za obaveštenja (mailer.py). Lozinka se ne šalje nazad stranici.
    def mail_cfg(self):
        return self.st.setdefault('mail', {'on': False, 'notify': True, 'host': '', 'port': 587, 'sec': 'starttls', 'user': '', 'pw': '',
                                           'from': '', 'from_name': 'Teco.Pi', 'to': []})

    def mail_view(self):
        c = self.mail_cfg()
        return {**{k: v for k, v in c.items() if k != 'pw'}, 'pw_set': bool(c.get('pw')), 'log': (getattr(self, 'mail_log', None) or [])[-10:]}

    async def mail_send(self, subject, body, to=None):
        import mailer
        c = self.mail_cfg()
        to = to or c.get('to') or []
        if not (c.get('host') and c.get('from') and to):
            return 'Email nije podešen (server, pošiljalac, primaoci).'
        err = await asyncio.get_running_loop().run_in_executor(None, mailer.send, c, to, subject, body)
        lg = self.__dict__.setdefault('mail_log', [])
        lg.append({'t': time.time(), 's': subject[:80], 'to': ', '.join(to), 'err': err})
        del lg[:-30]
        if err:
            log.warning('email: %s', err)
        self.mark()
        return err

    async def mail_cmd(self, c, a):
        import mailer
        m = self.mail_cfg()
        if c == 'mail_set':
            if a.get('preset') in mailer.PRESETS:
                m.update(mailer.PRESETS[a['preset']])
            for k in ('host', 'user', 'from', 'from_name'):
                if k in a:
                    m[k] = str(a[k] or '').strip()[:120]
            if a.get('pw'):   # prazno = zadrži sačuvanu lozinku
                m['pw'] = str(a['pw'])[:200]
            if 'port' in a:
                try:
                    m['port'] = max(1, min(65535, int(a['port'])))
                except (TypeError, ValueError):
                    return {'err': 'Port nije ispravan.'}
            if a.get('sec') in ('starttls', 'ssl', 'none'):
                m['sec'] = a['sec']
            if 'to' in a:
                to = [x.strip() for x in re.split(r'[,;\s]+', str(a['to'])) if x.strip()]
                bad = [x for x in to if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', x)]
                if bad:
                    return {'err': 'Neispravna adresa: ' + bad[0]}
                m['to'] = to[:10]
            if m.get('from') and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', m['from']):
                return {'err': 'Neispravna adresa pošiljaoca.'}
            for k in ('on', 'notify'):
                if k in a:
                    m[k] = bool(a[k])
            if m['on'] and not (m['host'] and m['from'] and m['to']):
                return {'err': 'Popuni SMTP server, adresu pošiljaoca i bar jednog primaoca.'}
            save_state(self.st)
            self.mark()
            return {'ok': 'Email podešavanje sačuvano'}
        err = await self.mail_send('Teco.Pi: probni mail', 'Ovo je probni mail sa Teco.Pi-ja.\nAko si ga dobio, obaveštenja e-mailom rade.\n\n— Teco.Pi, ' + time.strftime('%d.%m.%Y %H:%M'))
        return {'err': err} if err else {'ok': 'Probni mail poslat na ' + ', '.join(m['to'])}

    # ---------- Web monitor (Ekstenzije): da li javne web aplikacije rade (kao updown.io) — webmon.py
    # Pad se potvrđuje drugim pokušajem posle 2 s (bez lažnih uzbuna); SSL ističe -> obaveštenje (jednom dnevno).
    WEB_EVERY = (5, 10, 30, 60, 300)

    def web_cfg(self):
        return self.st.setdefault('web', {'mons': []})

    WEB_HIST = DATA / 'webmon.json'   # istorija (uptime po minutima, događaji, poslednje provere) — preživi restart

    def web_rt(self, mid):
        rts = self.__dict__.get('_web_rt')
        if rts is None:
            rts = self._web_rt = {}
            try:
                for k, v in json.loads(self.WEB_HIST.read_text()).items():
                    v['mins'] = {int(m): b for m, b in v.get('mins', {}).items()}
                    v['days'] = {int(d): b for d, b in v.get('days', {}).items()}
                    rts[k] = {'st': v.get('st'), 'since': v.get('since', 0), 'last': 0, 'ms': v.get('ms'), 'code': None, 'err': v.get('err'),
                              'hist': [h if isinstance(h, list) else [0, h] for h in v.get('hist', [])], 'mins': v['mins'], 'days': v['days'], 'events': v.get('events', []), 'ssl': v.get('ssl'), 'ssl_t': 0, 'busy': False}
            except (OSError, ValueError, AttributeError):
                pass
        return rts.setdefault(mid, {'st': None, 'since': 0, 'last': 0, 'ms': None, 'code': None, 'err': None, 'hist': [],
                                    'mins': {}, 'days': {}, 'events': [], 'ssl': None, 'ssl_t': 0, 'busy': False})

    def web_save(self):
        ids = {m['id'] for m in self.web_cfg()['mons']}
        data = {k: {x: v.get(x) for x in ('st', 'since', 'ms', 'err', 'hist', 'mins', 'days', 'events', 'ssl')} for k, v in (self.__dict__.get('_web_rt') or {}).items() if k in ids}
        try:
            tmp = self.WEB_HIST.with_suffix('.tmp')
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.WEB_HIST)
        except OSError as e:
            log.warning('web monitor istorija: %s', e)

    async def web_inet(self):
        """Da li Teco.Pi ima internet (pad sajta ili pad naše veze?). Keš 10 s."""
        c = getattr(self, '_web_inet', None)
        if c and time.time() - c[0] < 10:
            return c[1]

        async def tcp(host, port):
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
                w.close()
                return True
            except (OSError, asyncio.TimeoutError):
                return False
        ok = any(await asyncio.gather(tcp('1.1.1.1', 443), tcp('8.8.8.8', 53), tcp('9.9.9.9', 443)))
        self._web_inet = (time.time(), ok)
        return ok

    def web_view(self):
        out, now = [], time.time()
        for m in self.web_cfg()['mons']:
            r = self.web_rt(m['id'])
            mins = [v for k, v in r['mins'].items() if k >= int(now // 60) - 1440]   # 24 h po minutima: [ok, ukupno]
            tot = sum(v[1] for v in mins)
            days = r.get('days') or {}
            dks = sorted(days)[-30:]

            def upd(n):
                sel = [days[d] for d in sorted(days)[-n:]]
                t = sum(x[1] for x in sel)
                return round(100 * sum(x[0] for x in sel) / t, 2) if t else None
            out.append({**m, 'st': r['st'], 'since': r['since'], 'last': r['last'], 'ms': r['ms'], 'code': r['code'], 'err': r['err'],
                        'hist': r['hist'][-60:], 'up24': round(100 * sum(v[0] for v in mins) / tot, 2) if tot else None,
                        'up7': upd(7), 'up30': upd(30), 'days': [[d * 86400, round(100 * days[d][0] / days[d][1], 2) if days[d][1] else None] for d in dks],
                        'events': r['events'][-8:], 'ssl': r['ssl'], 'busy': r['busy']})
        return out

    def web_valid(self, a, m):
        from urllib.parse import urlsplit
        url = str(a.get('url', m.get('url', ''))).strip()
        if url and '://' not in url:
            url = 'https://' + url
        u = urlsplit(url)
        if u.scheme not in ('http', 'https') or not u.hostname:
            return 'Upiši adresu, npr. https://primer.rs'
        try:
            every = int(a.get('every', m.get('every', 30)))
            days = int(a.get('ssl_days', m.get('ssl_days', 14)))
        except (TypeError, ValueError):
            return 'Neispravan broj.'
        if every not in self.WEB_EVERY or not 1 <= days <= 90:
            return 'Neispravan interval ili broj dana.'
        m.update(url=url[:300], every=every, ssl_days=days, n=(str(a.get('n', m.get('n', ''))).strip() or u.hostname)[:40],
                 kw=str(a.get('kw', m.get('kw', ''))).strip()[:100], kw_not=bool(a.get('kw_not', m.get('kw_not', False))),
                 ssl=bool(a.get('ssl', m.get('ssl', u.scheme == 'https'))) and u.scheme == 'https', on=bool(a.get('on', m.get('on', True))))
        return None

    async def web_cmd(self, c, a):
        mons = self.web_cfg()['mons']
        if c == 'web_add':
            if len(mons) >= 30:
                return {'err': 'Najviše 30 sajtova.'}
            m = {'id': secrets.token_hex(4)}
            err = self.web_valid(a, m)
            if err:
                return {'err': err}
            mons.append(m)
            save_state(self.st)
            asyncio.get_running_loop().create_task(self.web_check(m, force_ssl=True))
            self.mark()
            return {'ok': 'Prati se ' + m['n']}
        m = next((x for x in mons if x['id'] == a.get('id')), None)
        if not m:
            return {'err': 'Sajt nije pronađen.'}
        if c == 'web_del':
            mons.remove(m)
            self.__dict__.get('_web_rt', {}).pop(m['id'], None)
            save_state(self.st)
            self.mark()
            return {'ok': 'Više se ne prati ' + m['n']}
        if c == 'web_set':
            err = self.web_valid(a, m)
            if err:
                return {'err': err}
            save_state(self.st)
            self.web_rt(m['id'])['ssl_t'] = 0   # nova adresa: SSL ponovo
        await self.web_check(m, force_ssl=True)
        r = self.web_rt(m['id'])
        return {'ok': '%s: %s' % (m['n'], 'radi · %d ms' % r['ms'] if r['st'] == 'up' else 'NE RADI · ' + (r['err'] or ''))}

    async def web_check(self, m, force_ssl=False):
        import webmon
        r = self.web_rt(m['id'])
        if r['busy']:
            return
        r['busy'] = True
        try:
            if not getattr(self, '_web_http', None) or self._web_http.closed:
                import aiohttp
                self._web_http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=60, force_close=True))
            res = await webmon.check(self._web_http, m['url'], m.get('kw', ''), m.get('kw_not', False))
            if not res['ok'] and r['st'] != 'down':   # potvrda pada: još jedan pokušaj posle 2 s
                await asyncio.sleep(2)
                res = await webmon.check(self._web_http, m['url'], m.get('kw', ''), m.get('kw_not', False))
            now = time.time()
            st = 'up' if res['ok'] else 'down'
            if not res['ok'] and not await self.web_inet():   # problem je naša veza, ne sajt
                st, res = 'net', {**res, 'err': 'Teco.Pi nema internet'}
            if st != r['st']:
                if r['st'] is not None and st != 'net' and not (st == 'up' and r['st'] == 'net'):   # bez interneta se ne javlja
                    msg = '%s %s' % (m['n'], 'ponovo radi.' if st == 'up' else 'NE RADI: ' + (res['err'] or '') + '.')
                    if st == 'up' and r['since']:
                        msg += ' (bio dole %s)' % self.web_dur(now - r['since'])
                    self.notify('web', msg, key='web:%s:%s' % (m['id'], st))
                    log.info('web monitor: %s', msg)
                r['events'] = (r['events'] + [{'t': now, 'st': st, 'err': res['err']}])[-30:]
                r['st'], r['since'] = st, now
                self._web_dirty = True
            r.update(last=now, ms=res['ms'], code=res['code'], err=res['err'])
            r['hist'] = (r['hist'] + [[int(now), res['ms'] if res['ok'] else -2 if st == 'net' else -1]])[-120:]
            if st != 'net':   # bez interneta: ne zna se da li sajt radi — ne računa se u uptime
                k = int(now // 60)
                b = r['mins'].setdefault(k, [0, 0])
                b[0] += res['ok']
                b[1] += 1
                lt = time.localtime(now)   # po danima (lokalno): 90 dana
                dk = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) // 86400)
                db = r.setdefault('days', {}).setdefault(dk, [0, 0])
                db[0] += res['ok']
                db[1] += 1
                if len(r['days']) > 95:
                    for old in sorted(r['days'])[:-90]:
                        del r['days'][old]
            if len(r['mins']) > 1500:
                for old in sorted(r['mins'])[:-1440]:
                    del r['mins'][old]
            if m.get('ssl') and (force_ssl or now - r['ssl_t'] > 6 * 3600):
                r['ssl_t'] = now
                r['ssl'] = await asyncio.get_running_loop().run_in_executor(None, webmon.cert_info, m['url'])
                sl = r['ssl']
                if sl.get('err'):
                    self.notify('web', '%s: %s' % (m['n'], sl['err']), key='web:%s:sslerr:%s' % (m['id'], time.strftime('%Y%m%d')))
                elif sl['days'] <= m.get('ssl_days', 14):
                    self.notify('web', '%s: SSL sertifikat ističe za %d dana (%s).' % (m['n'], sl['days'], time.strftime('%d.%m.%Y', time.localtime(sl['until']))),
                                key='web:%s:ssl:%s' % (m['id'], time.strftime('%Y%m%d')))
        except Exception:
            log.exception('web monitor %s', m.get('url'))
        finally:
            r['busy'] = False
            self.mark()

    @staticmethod
    def web_dur(sec):
        sec = int(sec)
        return '%d s' % sec if sec < 60 else '%d min' % (sec // 60) if sec < 3600 else '%d h %d min' % (sec // 3600, sec % 3600 // 60)

    async def web_loop(self):
        """Na 1 s: koji sajt je na redu (svaki ima svoj interval)."""
        await asyncio.sleep(5)
        saved = time.time()
        while True:
            try:
                now = time.time()
                if now - saved > 300 or getattr(self, '_web_dirty', False):
                    saved, self._web_dirty = now, False
                    await asyncio.get_running_loop().run_in_executor(None, self.web_save)
                for m in (list(self.web_cfg()['mons']) if self.web_cfg().get('on', True) else []):   # glavni prekidač
                    r = self.web_rt(m['id'])
                    if m.get('on', True) and not r['busy'] and now - r['last'] >= m.get('every', 30):
                        r['last'] = now   # da se ne pokrene dvaput dok traje
                        asyncio.get_running_loop().create_task(self.web_check(m))
            except Exception:
                log.exception('web monitor petlja')
            await asyncio.sleep(1)

    # ---------- Generator signala (Alati → Elektronika): 2 kanala na p3/p4 proširenja osciloskopa (siggen.py, pigpio)
    def gen_cfg(self):
        c = self.st.setdefault('gen', {})
        for ch, d in (('ch1', {'on': True, 'wave': 'square', 'freq': 1000.0, 'duty': 50.0}),
                      ('ch2', {'on': True, 'wave': 'tri', 'freq': 100.0, 'duty': 50.0})):
            c[ch] = {**d, **c.get(ch, {})}
        return c

    def gen_pins(self):
        from pins import PINS
        exp = self.tool_cfg('scope').get('exp') or 'exp2'
        return exp, PINS.get(exp, 'p3'), PINS.get(exp, 'p4')

    def gen_view(self):
        g = getattr(self, 'gen_rt', None) or {}
        exp, p1, p2 = self.gen_pins()
        return {**self.gen_cfg(), 'run': bool(g.get('run')), 'err': g.get('err'), 'freqs': g.get('freqs') or {},
                'exp': exp, 'pins': {'ch1': p1, 'ch2': p2}}

    async def gen_set(self, a):
        c = self.gen_cfg()
        for ch in ('ch1', 'ch2'):
            x = a.get(ch)
            if not isinstance(x, dict):
                continue
            if 'on' in x:
                c[ch]['on'] = bool(x['on'])
            if x.get('wave') in ('square', 'tri'):
                c[ch]['wave'] = x['wave']
            for k, lo, hi in (('freq', 1.0, 100000.0), ('duty', 1.0, 99.0)):
                if k in x:
                    try:
                        c[ch][k] = max(lo, min(hi, float(x[k])))
                    except (TypeError, ValueError):
                        return {'err': 'Neispravna vrednost: ' + k}
        save_state(self.st)
        if (getattr(self, 'gen_rt', None) or {}).get('run'):   # radi: novi talas odmah
            return await self.gen_run(True)
        if self.tool_running() == 'gen':
            self.tool_write('gen')
        self.mark()
        return {'ok': 'Sačuvano'}

    async def gen_run(self, on):
        import siggen
        rt = self.gen_rt = getattr(self, 'gen_rt', None) or {'gen': siggen.Gen()}
        loop = asyncio.get_running_loop()
        if not on:
            await loop.run_in_executor(None, rt['gen'].stop)
            await self._rc('sudo', '-n', 'systemctl', 'stop', 'pigpiod', timeout=15)   # servis ne troši procesor kad generator ne radi
            await self._rc('sudo', '-n', 'systemctl', 'reset-failed', 'pigpiod', timeout=10)   # pigpiod na SIGTERM izlazi sa greškom
            rt.update(run=False, err=None, freqs={})
            if self.tool_running() == 'gen':
                self.tool_write('gen')
            self.mark()
            return {'ok': 'Generator je isključen'}
        c = self.gen_cfg()
        _, p1, p2 = self.gen_pins()
        chs = [{'name': ch.upper(), 'gpio': g, 'wave': c[ch]['wave'], 'freq': c[ch]['freq'], 'duty': c[ch]['duty'], 'ch': ch}
               for ch, g in (('ch1', p1), ('ch2', p2)) if c[ch]['on'] and g]
        try:
            siggen.build(chs)   # proveri pre pokretanja servisa
        except siggen.GenError as e:
            rt.update(err=str(e))
            self.mark()
            return {'err': str(e)}
        if (await self._run('systemctl', 'is-active', 'pigpiod', timeout=5)).strip() != 'active':
            await self._rc('sudo', '-n', 'systemctl', 'start', 'pigpiod', timeout=20)
            await asyncio.sleep(1)
        try:
            freqs = await loop.run_in_executor(None, rt['gen'].start, chs)
        except Exception as e:
            rt.update(run=False, err=str(e))
            self.mark()
            return {'err': 'Generator: %s' % e}
        rt.update(run=True, err=None, freqs={x['ch']: f for x, f in zip(chs, freqs)})
        if self.tool_running() == 'gen':
            self.tool_write('gen')
        self.mark()
        return {'ok': 'Generator radi: ' + ', '.join('%s %s %s' % (x['name'], 'pravougaoni' if x['wave'] == 'square' else 'trougaoni', fmt_hz(f)) for x, f in zip(chs, freqs))}

    async def stor_mkdir(self, a):
        p = self.stor_disk(a.get('dev'))
        if not p or a.get('cat') not in self.STOR_DIRS:
            return {'err': 'Disk nije priključen.'}
        name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', str(a.get('n') or '')).strip(' .')[:80]
        if not name:
            return {'err': 'Upiši ime foldera.'}
        root = Path(p['mnt']) / self.STOR_DIRS[a['cat']]
        root.mkdir(parents=True, exist_ok=True)
        d = self.inside(root, a.get('path'))
        if not d or not d.is_dir():
            return {'err': 'Folder ne postoji.'}
        if (d / name).exists():
            return {'err': 'Već postoji: ' + name}
        try:
            (d / name).mkdir()
        except OSError as e:
            return {'err': 'Folder nije napravljen: %s' % e.strerror}
        return {'ok': 'Napravljen folder ' + name}

    # ---------- Alarmi (Smart Home): više centrala — Paradox (IP150, PAI most) i DSC (EnvisaLink, dsc_bridge.py).
    # Svaka centrala: data/alarm[-<id>].json (lozinke, chmod 600), svoj most (systemd korisnički servis) na 127.0.0.1:<port>.
    ALARM_MODELS = {'paradox': 'Paradox (IP150)', 'dsc': 'DSC (EnvisaLink)'}
    PARADOX_MODULES = ('IP150', 'IP100')   # isti protokol na portu 10000 (PAI, CONNECTION_TYPE IP), razlika je samo u nazivu
    ALARM_PANELS = {'paradox': ['EVO48', 'EVO96', 'EVO192', 'EVOHD', 'SP4000', 'SP5500', 'SP6000', 'SP65', 'SP7000', 'MG5000', 'MG5050'],
                    'dsc': ['PC1616', 'PC1832', 'PC1864', 'PC5010', 'PC5020', 'HS2016', 'HS2032', 'HS2064', 'HS2128']}

    @staticmethod
    def alarm_hash(code, salt):
        import hashlib
        return hashlib.pbkdf2_hmac('sha256', str(code).encode(), bytes.fromhex(salt), 100000).hex()

    def alarm_code_ok(self, aid, code):
        """Šifra za uključivanje/isključivanje sa stranice (ako je postavljena za taj alarm)."""
        c = self.alarm_cfg(aid)
        if not c.get('ui_hash'):
            return True
        import hmac
        return bool(code) and hmac.compare_digest(self.alarm_hash(code, c['ui_salt']), c['ui_hash'])

    def alarm_list(self):
        if 'alarms' not in self.st:   # ranije jedan Paradox (alarm.json) -> prvi u listi
            self.st['alarms'] = [{'id': 'a1', 'model': 'paradox'}] if ALARM_CFG.exists() else []
        return self.st['alarms']

    @staticmethod
    def alarm_file(aid):
        return ALARM_CFG if aid == 'a1' else DATA / ('alarm-%s.json' % aid)

    def alarm_model(self, aid):
        return next((x['model'] for x in self.alarm_list() if x['id'] == aid), 'paradox')

    def alarm_unit(self, aid):
        if self.alarm_model(aid) == 'dsc':
            return 'teco-dsc@%s.service' % aid
        return ALARM_UNIT if aid == 'a1' else 'teco-alarm@%s.service' % aid

    @staticmethod
    def alarm_port(aid):
        return 8098 if aid == 'a1' else 8110 + int(re.sub(r'\D', '', aid) or 0)

    def alarm_cfg(self, aid='a1'):
        try:
            return json.loads(self.alarm_file(aid).read_text())
        except (OSError, ValueError):
            return {}

    def alarm_write(self, aid, c):
        f = self.alarm_file(aid)
        f.write_text(json.dumps(c))
        os.chmod(f, 0o600)

    async def alarm_cfg_set(self, a):
        """Podešavanje centrale (stranica Alarm). Prazna lozinka/kod = zadrži sačuvano. Lozinke se nikad ne šalju nazad."""
        aid = str(a.get('id') or 'a1')
        if not any(x['id'] == aid for x in self.alarm_list()):
            return {'err': 'Alarm ne postoji.'}
        model, c = self.alarm_model(aid), self.alarm_cfg(aid)
        if model == 'paradox' and a.get('module') in self.PARADOX_MODULES:
            c['module'] = a['module']
        host = str(a.get('host') or '').strip()
        if a.get('enabled') and not re.fullmatch(r'[A-Za-z0-9.\-]{1,253}', host):
            return {'err': 'Upiši IP adresu %s (npr. 172.10.10.50).' % ('EnvisaLink-a' if model == 'dsc' else c.get('module', 'IP150') + ' modula')}
        try:
            port = int(a.get('port') or (4025 if model == 'dsc' else 10000))
            assert 1 <= port <= 65535
        except (ValueError, AssertionError):
            return {'err': 'Port nije ispravan (Paradox 10000, DSC 4025).'}
        c.update(host=host, port=port, enabled=bool(a.get('enabled')), name=str(a.get('name') or '').strip()[:40], api_port=self.alarm_port(aid))
        keys = ('tpi_password', 'code') if model == 'dsc' else ('ip_password', 'pc_password')
        for k in keys:
            if a.get(k):
                c[k] = str(a[k])[:32]
        if 'panel' in a:
            c['panel'] = str(a.get('panel') or '')[:20]
        if a.get('ui_code_clear'):   # šifra za stranicu: ukloni
            c.pop('ui_hash', None), c.pop('ui_salt', None)
        elif a.get('ui_code'):
            code = str(a['ui_code']).strip()
            if not re.fullmatch(r'\d{4,8}', code):
                return {'err': 'Šifra za stranicu: 4 do 8 cifara.'}
            c['ui_salt'] = secrets.token_hex(16)
            c['ui_hash'] = self.alarm_hash(code, c['ui_salt'])
        if model == 'dsc':
            for k, lo, hi in (('partitions', 1, 8), ('zones', 1, 64)):
                try:
                    c[k] = max(lo, min(hi, int(a.get(k) or c.get(k) or (1 if k == 'partitions' else 8))))
                except (TypeError, ValueError):
                    return {'err': 'Neispravan broj: ' + k}
        self.alarm_write(aid, c)
        unit = self.alarm_unit(aid)
        if c['enabled']:
            await self._run('systemctl', '--user', 'enable', unit, timeout=20)
            await self._run('systemctl', '--user', 'restart', unit, timeout=20)
        else:
            await self._run('systemctl', '--user', 'disable', '--now', unit, timeout=20)
        self.__dict__.setdefault('alarms_st', {})[aid] = {}
        self.mark()
        return {'ok': 'Alarm: podešavanje sačuvano' + (', povezujem se…' if c['enabled'] else ' (isključen).')}

    async def alarm_add(self, a):
        model = a.get('model')
        if model not in self.ALARM_MODELS:
            return {'err': 'Izaberi model (Paradox ili DSC).'}
        lst = self.alarm_list()
        n = max([int(re.sub(r'\D', '', x['id']) or 0) for x in lst] + [1]) + 1 if lst else 1
        aid = 'a%d' % n
        lst.append({'id': aid, 'model': model})
        self.alarm_write(aid, {'enabled': False, 'name': str(a.get('name') or '').strip()[:40], 'api_port': self.alarm_port(aid),
                               'port': 4025 if model == 'dsc' else 10000})
        save_state(self.st)
        self.mark()
        return {'ok': 'Dodat alarm (%s) — upiši adresu i lozinke' % self.ALARM_MODELS[model], 'id': aid}

    async def alarm_del(self, aid):
        lst = self.alarm_list()
        x = next((x for x in lst if x['id'] == aid), None)
        if not x:
            return {'err': 'Alarm ne postoji.'}
        await self._run('systemctl', '--user', 'disable', '--now', self.alarm_unit(aid), timeout=20)
        lst.remove(x)
        save_state(self.st)
        if aid != 'a1':   # a1: podešavanje i dnevnik ostaju (stari fajlovi), ostali se brišu
            for f in (self.alarm_file(aid), DATA / ('alarm-events-%s.json' % aid), DATA / ('pai-config-%s.json' % aid)):
                f.unlink(missing_ok=True)
        self.__dict__.setdefault('alarms_st', {}).pop(aid, None)
        self.mark()
        return {'ok': 'Alarm je uklonjen'}

    async def alarm_post(self, path, body, aid='a1'):
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
                async with s.post('http://127.0.0.1:%d%s' % (self.alarm_port(aid), path), json=body) as r:
                    return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return {'err': 'Alarm servis ne odgovara.'}

    async def alarm_extra(self, c, a):
        """Monitor zone (dnevnik), brisanje memorije alarma, brisanje dnevnika."""
        aid = str(a.get('id') or 'a1')
        if c == 'alarm_monitor':
            cfg = self.alarm_cfg(aid)
            mon = {int(x) for x in cfg.get('monitor', [])}
            z = int(a.get('zone'))
            (mon.add if a.get('on') else mon.discard)(z)
            cfg['monitor'] = sorted(mon)
            self.alarm_write(aid, cfg)
            self.mark()
            return {'ok': 'Zona se ' + ('prati: događaji se čuvaju' if a.get('on') else 'više ne prati')}
        if c == 'alarm_mem_clear':
            r = await self.alarm_post('/zone', {'p': 'all', 'cmd': 'clear_alarm_memory'}, aid)
            return {'ok': 'Memorija alarma je obrisana'} if r.get('ok') else {'err': r.get('err') or 'Nije uspelo.'}
        if c == 'alarm_panic' and not self.alarm_code_ok(aid, a.get('code')):
            await asyncio.sleep(1)
            return {'err': 'Pogrešna šifra alarma.', 'code_req': True}
        if c == 'alarm_panic':   # panic (kao 1+3 / 4+6 / 7+9 na šifratoru)
            t = a.get('type') if a.get('type') in ('emergency', 'medical', 'fire') else 'emergency'
            log.warning('PANIC sa stranice: %s', t)
            r = await self.alarm_post('/panic', {'type': t, 'p': a.get('p') or 1}, aid)
            return {'ok': 'PANIC poslat centrali'} if r.get('ok') else {'err': r.get('err') or 'Nije uspelo.'}
        if c == 'alarm_time':   # sat centrale = sat Teco.Pi-ja
            r = await self.alarm_post('/time', {}, aid)
            if not r.get('ok'):
                return {'err': r.get('err') or 'Nije uspelo.'}
            d = r.get('drift')
            return {'ok': 'Sat centrale podešen: ' + r['time'] + ('' if d is None or abs(d) < 60 else ' (kasnio je %d min)' % (d // 60) if d > 0 else ' (žurio je %d min)' % (-d // 60))}
        if c == 'alarm_ev_clear':
            r = await self.alarm_post('/events/clear', {}, aid)
            return {'ok': 'Dnevnik događaja je obrisan'} if r.get('ok') else {'err': r.get('err') or 'Nije uspelo.'}
        if c == 'alarm_events':   # dnevnik za izabrani period (ceo dnevnik na mostu, ne samo poslednjih 100)
            try:
                t0, t1 = int(a.get('from') or 0), int(a.get('to') or 0)
            except (TypeError, ValueError):
                return {'err': 'Neispravan period.'}
            r = await self.alarm_post('/events/range', {'from': t0, 'to': t1}, aid)
            return {'events': r.get('events') or []} if 'events' in r else {'err': r.get('err') or 'Nije uspelo.'}

    async def alarm_do(self, a, trusted=False):
        """Particija (uključi u režimu Regular/Stay/Instant/Force, isključi) ili PGM izlaz (a['pgm'])."""
        cmd = str(a.get('cmd') or '')
        pgm = a.get('pgm') is not None
        if cmd not in (PGM_CMDS if pgm else ALARM_CMDS):
            return {'err': 'Nepoznata komanda.'}
        if not pgm and not trusted and not self.alarm_code_ok(str(a.get('id') or 'a1'), a.get('code')):
            await asyncio.sleep(1)   # uspori pogađanje
            log.warning('alarm %s: pogrešna šifra (%s)', a.get('id'), cmd)
            return {'err': 'Pogrešna šifra alarma.', 'code_req': True}
        import aiohttp
        path, target = ('/pgm', str(a.get('pgm'))) if pgm else ('/partition', str(a.get('p') or 'all'))
        api = 'http://127.0.0.1:%d' % self.alarm_port(str(a.get('id') or 'a1'))

        async def post(c):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post(api + path, json={'p': target, 'cmd': c}) as r:
                    return await r.json()
        try:
            res = await post('on' if cmd == 'pulse' else cmd)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return {'err': 'Alarm servis ne odgovara.'}
        if cmd == 'pulse' and res.get('ok'):
            # „aktiviraj na 5 s“: vraćanje radi server (i ako se stranica zatvori). 'release', ne 'off':
            # 'off' (override_off) ostavlja PGM zaključan na isključeno, pa ga ni šifrator/Insite Gold ne bi okinuli
            async def later():
                await asyncio.sleep(PGM_PULSE)
                try:
                    r2 = await post('release')
                    log.info('alarm pgm: %s release posle %s s -> %s', target, PGM_PULSE, r2)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                    log.warning('alarm pgm: %s release nije uspeo: %s', target, e)
            asyncio.get_running_loop().create_task(later())
        log.info('alarm %s: %s %s -> %s', path[1:], target, cmd, res)
        self.alarm_poll_now = True
        if not res.get('ok'):
            return {'err': res.get('err') or 'Komanda nije uspela.'}
        return {'ok': ('PGM ' + target + ': ' + PGM_CMDS[cmd]) if pgm else 'Alarm: ' + ALARM_CMDS[cmd]}

    async def alarm_loop(self):
        """Za svaku centralu: dugo čekanje na njen most (/wait?v=) — promena (zona, particija, smetnja) stiže odmah."""
        self.alarm, self.alarms_st = {}, {}
        tasks = {}
        while True:
            ids = [x['id'] for x in self.alarm_list()]
            for aid in ids:
                if aid not in tasks or tasks[aid].done():
                    tasks[aid] = asyncio.get_running_loop().create_task(self.alarm_watch(aid))
            for aid in [k for k in tasks if k not in ids]:
                tasks.pop(aid).cancel()
                self.alarms_st.pop(aid, None)
                self.mark()
            prim = self.alarms_st.get(ids[0]) if ids else {}
            if (prim or {}) is not self.alarm:
                self.alarm = prim or {}
            await asyncio.sleep(2)

    async def alarm_watch(self, aid):
        import aiohttp
        v = -1
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            while True:
                if not self.alarm_cfg(aid).get('enabled'):
                    if self.alarms_st.get(aid):
                        self.alarms_st[aid] = {}
                        self.mark()
                    v = -1
                    await asyncio.sleep(2)
                    continue
                try:
                    async with s.get('http://127.0.0.1:%d/wait' % self.alarm_port(aid), params={'v': v}) as r:
                        st = await r.json()
                    v = st.pop('v', -1)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    st = {'run': 'down', 'err': 'Alarm servis se pokreće…', 'partitions': [], 'zones': []}
                    v = -1
                    await asyncio.sleep(2)
                if st != self.alarms_st.get(aid):
                    self.alarms_st[aid] = st
                    lst = self.alarm_list()
                    if lst and lst[0]['id'] == aid:   # glavni alarm: interfoni, PGM obaveštenja, SMS status
                        self.alarm = st
                        self.pgm_events(st)
                    elif st.get('run') == 'run':
                        try:
                            self.notify_alarm(st, aid)
                        except Exception:
                            log.exception('obaveštenje alarma %s', aid)
                    self.mark()

    def pgm_events(self, st):
        """Obaveštenja vezana za PGM alarma: PGM se uključi -> pokreni obaveštenje (zvono, interfon…)."""
        if st.get('run') != 'run':
            return
        try:
            lst = self.alarm_list()
            self.notify_alarm(st, lst[0]['id'] if lst else 'a1')   # SMS obaveštenja: alarm, uključen/isključen, struja
        except Exception:
            log.exception('obaveštenje alarma')
        # interfoni: zona alarma se otvori (zvoni) -> obaveštenje + zapamti vreme poziva
        zc = {z.get('id'): bool(z.get('open')) for z in st.get('zones', [])}
        zp = getattr(self, '_zone_prev', None)
        self._zone_prev = zc
        if zp is not None:
            for dv in self.st.get('devices', []):
                if dv.get('type') not in RING_TYPES:
                    continue
                ic = dv.get('intercom') or {}
                zid = int(ic.get('zone') or 0)
                if zid and zc.get(zid) and not zp.get(zid):
                    if not hasattr(self, 'ring_last'):
                        self.ring_last = {}
                    self.ring_last[dv['id']] = int(time.time())
                    log.info('%s %s zvoni (zona %s)', DEV_TYPES[dv['type']].lower(), dv.get('name'), zid)
                    ev = ic.get('event') or ('zvono' if dv['type'] == 'bell' else 'interfon')
                    if self.st.get('events', {}).get(ev, {}).get('on', True):
                        asyncio.get_running_loop().create_task(self.fire_event(ev))
        # (ranije je i obaveštenje imalo svoj PGM okidač; sada okidač određuje samo uređaj — zvono/interfon i njegova zona)

    def alarm_snapshot(self, aid='a1'):
        c = self.alarm_cfg(aid)
        st = (getattr(self, 'alarms_st', None) or {}).get(aid) or {}
        model = self.alarm_model(aid)
        zk = ('id', 'key', 'label', 'open', 'bypassed', 'alarm', 'tamper', 'fire', 'low_battery', 'generated_alarm', 'was_in_alarm',
              'supervision_trouble', 'in_tx_delay', 'shutdown', 'fire_alarm')
        pk = ('id', 'key', 'label', 'arm', 'arm_stay', 'arm_away', 'arm_sleep', 'arm_no_entry', 'alarm', 'current_state', 'target_state', 'ready',
              'ready_status', 'exit_delay', 'entry_delay', 'fire_alarm', 'audible_alarm', 'silent_alarm', 'alarm_in_memory',
              'trouble', 'zone_bypassed', 'panic_alarm', 'bell')
        sysd = st.get('system') or {}
        tr = sysd.get('troubles') or {}
        parts = st.get('partitions', [])
        return {
            'id': aid, 'model': model, 'model_n': 'Paradox (%s)' % c.get('module', 'IP150') if model == 'paradox' else self.ALARM_MODELS[model],
            'name': c.get('name') or (parts[0].get('label') if parts else '') or 'Alarm',
            'on': bool(c.get('enabled')),
            'cfg': {'host': c.get('host', ''), 'port': c.get('port', 4025 if model == 'dsc' else 10000), 'name': c.get('name', ''),
                    'ip_pw': bool(c.get('ip_password')), 'pc_pw': bool(c.get('pc_password')),
                    'tpi_pw': bool(c.get('tpi_password')), 'code': bool(c.get('code')), 'partitions': c.get('partitions', 1), 'zones': c.get('zones', 8),
                    'panel': c.get('panel', ''), 'ui_code': bool(c.get('ui_hash')), 'module': c.get('module', 'IP150')},
            'panels': self.ALARM_PANELS[model],
            'troubles': sorted(k for k, v in tr.items() if v is True and not k.startswith('_')),
            'power': {k: sysd.get('power', {}).get(k) for k in ('vdc', 'battery', 'dc') if isinstance(sysd.get('power', {}).get(k), (int, float))},
            'events': (st.get('events') or [])[-100:],
            'monitor': [int(x) for x in c.get('monitor', [])],
            'run': st.get('run'), 'err': st.get('err'), 'panel': st.get('panel'), 'site': st.get('site') or {},
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
                title = (await self.resolve(url, fast=True))[0]   # priprema (keš); ako za to vreme stigne novi zahtev, ovaj je zastareo
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
            new = str(a.get('name') or 'Telefon')[:40] if a.get('on') else None
            if new != getattr(self, 'cast_sender', None):
                log.info('cast: %s', ('povezan ' + new) if new else 'telefon odvojen')
            self.cast_sender = new
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

    @staticmethod
    def _tone_wav(path, key):
        """Sistemska melodija: zvono (osnovni ton + harmonici, eksponencijalno gašenje)."""
        import math
        import struct
        import wave
        rate, lead = 48000, 0.12   # 48 kHz kao izlaz (bez preračunavanja); kratka tišina dok se izlaz (HDMI/BT) ne probudi
        E4, G4, C5, D5, E5, G5, B5 = 329.63, 392.0, 523.25, 587.33, 659.25, 783.99, 987.77
        notes = {   # (početak s, frekvencija, trajanje s)
            'dingdong': [(0, E5, 1.6), (0.6, C5, 2.0)],
            'seven': [(0, B5, 0.9), (0.32, G5, 1.6), (1.4, B5, 0.9), (1.72, G5, 1.8)],   # „ting-tong“ elektronsko zvono, dva puta
            'westminster': [(i * 0.5, f, 1.6) for i, f in enumerate((E5, C5, D5, G4))] + [(2.4 + i * 0.5, f, 1.8 if i == 3 else 1.6) for i, f in enumerate((G4, D5, E5, C5))],
            'trio': [(0, C5, 1.4), (0.35, E5, 1.4), (0.7, G5, 2.0)],
        }[key]
        notes = [(s + lead, f, d) for s, f, d in notes]
        bright = key == 'seven'
        n = int(rate * (max(s + d for s, _, d in notes) + 0.1))
        buf = [0.0] * n
        for s, f, d in notes:
            i0, m = int(s * rate), int(d * rate)
            parts = ((1, 1), (2, 0.08), (3, 0.03)) if bright else ((1, 1), (2, 0.35), (3, 0.12), (4.2, 0.06))
            for i in range(min(m, n - i0)):
                t = i / rate
                env = min(1, i / 60) * math.exp(-t * (3.2 if bright else 2.4))
                buf[i0 + i] += env * sum(a * math.sin(2 * math.pi * f * h * t) for h, a in parts)
        peak = max(abs(x) for x in buf) or 1
        pcm = b''.join(struct.pack('<h', int(26000 * x / peak)) for x in buf)
        voice = STATIC / 'sounds' / 'seven-hello.wav'   # 7-Eleven: na kraju glas „Hello, welcome!“ (48 kHz mono)
        if key == 'seven' and voice.exists():
            try:
                with wave.open(str(voice)) as v:
                    vr = v.readframes(v.getnframes()) if (v.getframerate(), v.getnchannels(), v.getsampwidth()) == (rate, 1, 2) else b''
                s = struct.unpack('<%dh' % (len(vr) // 2), vr)
                loud = [i for i, x in enumerate(s) if abs(x) > 400]   # bez tišine na početku i kraju snimka
                if loud:   # glas kreće dok poslednji ton još zvoni (2,5 s), zvuci se sabiraju
                    t = list(struct.unpack('<%dh' % (len(pcm) // 2), pcm))
                    vs, at = s[loud[0]:loud[-1] + 1], int(rate * 2.5)
                    t += [0] * max(0, at + len(vs) + int(rate * 0.3) - len(t))
                    for i, x in enumerate(vs):
                        t[at + i] = max(-32767, min(32767, int(t[at + i] * 0.6 + x * 0.95)))
                    pcm = struct.pack('<%dh' % len(t), *t)
            except (OSError, wave.Error, struct.error):
                pass
        with wave.open(str(path), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)

    def event_sound(self, eid):
        """Zvuk u folderu događaja: kod alarma ugrađen alarm.wav (ranije i otpremljen zvuk zvona, prebacuje se u Zvukove)."""
        d = EVENTS_DIR / eid
        files = sorted(p for p in d.glob('*') if p.suffix.lower() in AUDIO_EXT) if d.exists() else []
        return files[0] if files else None

    @staticmethod
    def tones_list():
        """Skladište → Zvukovi: ugrađene melodije prve, pa otpremljeni fajlovi."""
        fs = [p for p in TONES_DIR.glob('*') if p.suffix.lower() in AUDIO_EXT and not p.name.startswith('.')] if TONES_DIR.exists() else []
        sys_ = {k + '.wav': n for k, n in EVENT_TONES.items()}
        fs.sort(key=lambda p: (p.name not in sys_, list(sys_).index(p.name) if p.name in sys_ else 0, p.name.lower()))
        out = []
        for p in fs:
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({'f': p.name, 'n': sys_.get(p.name, p.stem), 'sys': p.name in sys_, 'size': st.st_size, 't': int(st.st_mtime)})
        return out

    def event_tone(self, eid):
        """Izabrana melodija (ime fajla u Zvukovima); nema je više → ding-dong."""
        t = str((self.st['events'].get(eid) or {}).get('tone') or '')
        if t in EVENT_TONES:   # ranije: ključ ugrađene melodije
            t += '.wav'
        return t if t and '/' not in t and (TONES_DIR / t).is_file() else 'dingdong.wav'

    def event_play(self, eid, tone=None):
        if eid in ALARMS:
            return self.event_sound(eid)
        p = TONES_DIR / (tone or self.event_tone(eid))
        if not p.exists() and p.stem in EVENT_TONES:
            TONES_DIR.mkdir(parents=True, exist_ok=True)
            self._tone_wav(p, p.stem)
        return p if p.exists() else None

    async def play_sound(self, path, loop=False):
        """Kratak zvuk preko PipeWire-a. pw-play je lagan (brz start, bez zastoja na Pi 3B); mpv samo za petlju i m4a/aac/opus."""
        vol = max(int(self.mpv.props.get('volume') or 70), 60)
        env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/%d' % os.getuid())
        if not loop and path.suffix.lower() in ('.wav', '.mp3', '.ogg', '.flac'):
            cmd = ['pw-play', '--volume', '%.2f' % (vol / 100), str(path)]
        else:
            cmd = ['mpv', '--no-config', '--no-video', '--ao=pipewire', '--really-quiet', '--volume=%d' % vol, *(['--loop-file=inf'] if loop else []), str(path)]
        return await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env)

    async def events_assets(self):
        """Folder i slika (image.png, za web pregled) za svaki događaj; slika se osvežava kad se promeni naziv."""
        TONES_DIR.mkdir(parents=True, exist_ok=True)
        for t in EVENT_TONES:   # ugrađene melodije (jednom, u pozadini)
            if not (TONES_DIR / (t + '.wav')).exists():
                await asyncio.to_thread(self._tone_wav, TONES_DIR / (t + '.wav'), t)
        for eid, ev in self.st['events'].items():   # ranije otpremljen zvuk zvona/interfona -> Zvukovi, ostaje izabran
            old = self.event_sound(eid) if eid not in ALARMS else None
            if old:
                dest = self.free_name(TONES_DIR / ('%s%s' % (ev['n'], old.suffix.lower())))
                shutil.move(str(old), dest)
                if ev.get('tone') in (None, 'custom'):
                    ev['tone'] = dest.name
                save_state(self.st)
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
        snd, proc = self.event_play(eid), None
        if snd:
            proc = await self.play_sound(snd, loop=eid in ALARMS)

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
            out.append({'id': eid, 'n': ev['n'], 'on': ev.get('on', True), 'sec': ev['sec'], 'pause': ev.get('pause', True),
                        'dir': str(EVENTS_DIR / eid), 'alarm': eid in ALARMS, 'pgm': ev.get('pgm'), 'tone': self.event_tone(eid)})
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
        await self.ntp_refresh()   # sat: chrony (Ekstenzije → NTP vreme)
        wifi = await self._run('nmcli', '-t', '-f', 'ACTIVE,SSID,SIGNAL', 'dev', 'wifi', 'list', '--rescan', 'no', timeout=5)
        for line in wifi.splitlines():
            if line.startswith('yes:'):
                _, net['ssid'], net['signal'] = line.split(':', 2)
        # Wi-Fi: podešavanje adrese za trenutnu Wi-Fi mrežu
        wcon = await self._wifi_con()
        if wcon:
            out = await self._run('nmcli', '-t', '-f', 'ipv4.method,ipv4.addresses,ipv4.gateway', 'con', 'show', wcon, timeout=5)
            cfg = dict(l.split(':', 1) for l in out.splitlines() if ':' in l)
            net['wlan'] = {'con': wcon, 'mode': 'static' if cfg.get('ipv4.method') == 'manual' else 'dhcp',
                           'ip': cfg.get('ipv4.addresses', ''), 'gw': cfg.get('ipv4.gateway', '').replace('--', '')}
        net['radio'] = (await self._run('nmcli', 'radio', 'wifi', timeout=5)).strip() == 'enabled'
        eth_up = any(i['n'] == 'eth0' and i['up'] and i['ip'] for i in net['ifs'])
        net['pref'] = self.st.get('net_pref') or ('eth' if eth_up else 'wifi')
        self.net = net
        self.mark()

    async def _eth_con(self):
        out = await self._run('nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show', timeout=5)
        for line in out.splitlines():
            name, _, typ = line.rpartition(':')
            if typ == '802-3-ethernet':
                return name.replace('\\:', ':')
        return None

    async def net_direct(self, on):
        """PS2 kablom direktno na Pi: eth0 = 192.168.50.1/24 bez gateway-a (internet i kućna mreža ostaju na Wi-Fi-ju).
        Isključeno: kabl se vraća na DHCP (ruter)."""
        if on and not (self.net.get('pref') == 'wifi' and self.net.get('ssid')):
            return {'err': 'Prvo prebaci Pi na Wi-Fi (Mreža → Wi-Fi), pa kabl ostaje slobodan za PS2.'}
        con = await self._eth_con()
        if not con:
            return {'err': 'Kablovska veza nije pronađena.'}
        if on:
            args = ['ipv4.method', 'manual', 'ipv4.addresses', DIRECT_IP, 'ipv4.gateway', '', 'ipv4.dns', '',
                    'ipv4.never-default', 'yes', 'ipv4.ignore-auto-dns', 'yes']
        else:
            args = ['ipv4.method', 'auto', 'ipv4.addresses', '', 'ipv4.gateway', '', 'ipv4.never-default', 'no', 'ipv4.ignore-auto-dns', 'no']
        out = await self._run('nmcli', 'con', 'mod', con, *args, timeout=10)
        if 'Error' in out:
            return {'err': 'Nije sačuvano: ' + out.strip()[:120]}
        self.st['net_direct'] = on
        save_state(self.st)
        await self._run('nmcli', 'con', 'up', con, timeout=20)   # bez kabla ne uspe: NetworkManager je podigne kad se kabl priključi
        await self.net_refresh()
        return {'ok': 'PS2 direktno: Pi je 192.168.50.1 na kablu' if on else 'Kabl je ponovo za ruter (DHCP)'}

    async def _wifi_con(self):
        """Wi-Fi veza koja je trenutno aktivna (ili None)."""
        out = await self._run('nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show', '--active', timeout=5)
        for line in out.splitlines():
            f = self._nm_split(line)
            if len(f) == 2 and f[1] == '802-11-wireless':
                return f[0]
        return None

    async def net_mode(self, mode):
        """Kabl ili Wi-Fi. Kabl: Wi-Fi je isključen (jedna adresa, tecopi.local uvek pokazuje na nju).
        Wi-Fi: Wi-Fi uključen i ima prednost; kabl, ako je priključen, ostaje kao rezerva."""
        if mode not in ('eth', 'wifi'):
            return {'err': 'Nepoznat način.'}
        eth = next((i for i in self.net.get('ifs', []) if i['n'] == 'eth0'), {})
        if mode == 'eth' and self.st.get('net_direct'):
            return {'err': 'Kabl je sada za PS2 (direktna veza). Prvo je isključi u Deljenju fajlova.'}
        if mode == 'eth' and not (eth.get('up') and eth.get('ip')):
            return {'err': 'Kabl nije priključen (eth0 nema adresu). Wi-Fi ostaje uključen.'}
        self.st['net_pref'] = mode
        save_state(self.st)
        econ = await self._eth_con()
        wifis = await self._wifi_saved()
        em, wm = ('100', '600') if mode == 'eth' else ('700', '100')   # manja metrika = podrazumevani izlaz
        if econ:
            await self._run('nmcli', 'con', 'mod', econ, 'ipv4.route-metric', em, timeout=10)
        for w in wifis:
            await self._run('nmcli', 'con', 'mod', 'id', w, 'ipv4.route-metric', wm, timeout=10)
        if mode == 'eth':
            await self._run('nmcli', 'radio', 'wifi', 'off', timeout=10)
            await self._run('nmcli', 'dev', 'reapply', 'eth0', timeout=15)
        else:
            await self._run('nmcli', 'radio', 'wifi', 'on', timeout=10)
            if econ and eth.get('up'):
                await self._run('nmcli', 'dev', 'reapply', 'eth0', timeout=15)

        async def later():   # Wi-Fi se poveže za par sekundi
            await asyncio.sleep(8)
            await self.net_refresh()
            if mode == 'wifi':
                await self.wifi_scan()
        asyncio.get_running_loop().create_task(later())
        await self.net_refresh()
        return {'ok': 'Mreža: kabl (Wi-Fi isključen)' if mode == 'eth' else 'Mreža: Wi-Fi' + (' · kabl je rezerva' if eth.get('up') else '')}

    # ---------- Wi-Fi (nmcli): traženje mreža, povezivanje, brisanje sačuvanih
    @staticmethod
    def _nm_split(line):
        """nmcli -t deli polja sa ':' a ':' u vrednosti piše kao '\\:'."""
        return [p.replace('\x00', ':') for p in line.replace('\\:', '\x00').split(':')]

    async def wifi_an(self, rescan=True):
        """Alati → Wi-Fi analizator: sve okolne pristupne tačke (i skrivene) sa kanalom, jačinom i proizvođačem,
        plus preporuka kanala: 2,4 GHz samo 1/6/11 (ne preklapaju se), 5 GHz kanali bez DFS-a; težina = snaga signala."""
        import netscan
        if getattr(self, 'wifi_an_busy', False):
            return {'err': 'Skeniranje je već u toku.'}
        self.wifi_an_busy = True
        try:
            out = await self._run('nmcli', '-t', '-f', 'IN-USE,SSID,BSSID,CHAN,FREQ,RATE,SIGNAL,SECURITY', 'dev', 'wifi', 'list',
                                  '--rescan', 'yes' if rescan else 'no', timeout=30)
        finally:
            self.wifi_an_busy = False
        nets = []
        for line in out.splitlines():
            f = self._nm_split(line)
            if len(f) < 8:
                continue
            freq = int(re.sub(r'\D', '', f[4]) or 0)
            sig = int(f[6]) if f[6].isdigit() else 0
            ch = int(f[3]) if f[3].isdigit() else 0
            nets.append({'ssid': f[1], 'bssid': f[2].lower(), 'ch': ch, 'band': '5' if freq > 4000 else '2.4', 'freq': freq, 'rate': f[5],
                         'sig': sig, 'dbm': round(sig / 2 - 100), 'sec': '' if f[7] in ('', '--') else f[7], 'on': f[0] == '*',
                         'vendor': netscan.vendor(f[2]) or ''})
        nets.sort(key=lambda n: (not n['on'], -n['sig']))
        mine = {n['ssid'] for n in nets if n['on'] and n['ssid']}

        def score(c, band):   # koliko tuđi signali smetaju kanalu c (linearna snaga, preklapanje po razmaku kanala)
            s = 0.0
            for n in nets:
                if n['band'] != band or n['ssid'] in mine:
                    continue
                d = abs(n['ch'] - c)
                ov = max(0.0, 1 - d / 5) if band == '2.4' else (1.0 if d == 0 else 0.5 if d <= 4 else 0.0)
                s += ov * 10 ** (n['dbm'] / 10)
            return s
        rec = {}
        for band, cands in (('2.4', [1, 6, 11]), ('5', [36, 40, 44, 48, 149, 153, 157, 161])):
            if band == '5' and not any(n['band'] == '5' for n in nets):
                continue
            sc = {c: score(c, band) for c in cands}
            mx = max(sc.values()) or 1
            best = min(sc, key=sc.get)
            rec[band] = {'best': best, 'load': {str(c): round(v / mx * 100) for c, v in sc.items()},
                         'cnt': {str(c): sum(1 for n in nets if n['band'] == band and n['ssid'] not in mine and abs(n['ch'] - c) < (5 if band == '2.4' else 1)) for c in cands}}
        cur = next((n for n in nets if n['on']), None)
        self.wifi_last = {'nets': nets, 'rec': rec, 'cur': cur, 't': time.time(), 'radio': bool(self.net.get('radio', True))}
        if self.tool_running() == 'wifi':
            self.tool_write('wifi')   # TV prikaz se osveži
        return self.wifi_last

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
            await self._run('nmcli', 'con', 'mod', 'id', ssid, 'ipv6.method', 'disabled',
                            'ipv4.route-metric', '100' if self.st.get('net_pref') == 'wifi' else '600', timeout=10)
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
        wifi = a.get('if') == 'wifi'
        if not wifi and self.st.get('net_direct'):
            return {'err': 'Kabl je sada za PS2 (direktna veza). Prvo je isključi u Deljenju fajlova.'}
        con = await (self._wifi_con() if wifi else self._eth_con())
        if not con:
            return {'err': 'Prvo se poveži na Wi-Fi mrežu.' if wifi else 'Kablovska veza nije pronađena.'}
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
        return {'ok': '%s: %s sačuvana. Ako se stranica ne vrati, otvori http://tecopi.local' % ('Wi-Fi' if wifi else 'Kabl', msg)}

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

    # ---------- NTP vreme (Ekstenzije): chrony — Pi uzima vreme (Srbija / Cloudflare / svoj) i po želji ga deli mreži
    def ntp_cfg(self):
        c = self.st.setdefault('ntp', {'servers': list(NTP_PRESETS['rs']), 'serve': False})
        c.setdefault('client', True)
        if c['servers'] not in NTP_PRESETS.values():   # samo dva izvora: Srbija ili Cloudflare
            c['servers'] = list(NTP_PRESETS['rs'])
        return c

    async def ntp_write(self):
        c = self.ntp_cfg()
        lines = ['# generiše Teco.Pi (Ekstenzije → NTP vreme) — ne menjati ručno']
        if c.get('client'):   # NTP klijent: Pi uzima vreme sa izabranog izvora
            for s in c['servers']:
                lines.append('%s %s iburst' % ('pool' if 'pool' in s else 'server', s))
        if c.get('serve'):   # NTP server za kućnu mrežu i VPN; „local“: daje vreme i kad nema interneta (sat ide dalje sam)
            lines += ['allow 10.0.0.0/8', 'allow 172.16.0.0/12', 'allow 192.168.0.0/16', 'local stratum 10']
        if not c.get('client') and not c.get('serve'):   # oba isključena: chrony ne treba
            await self._run('sudo', '-n', 'systemctl', 'disable', '--now', 'chrony', timeout=20)
            self.ntp_st = {}
            return None
        await self._run('sudo', '-n', 'systemctl', 'enable', 'chrony', timeout=20)
        p = await asyncio.create_subprocess_exec('sudo', '-n', 'tee', str(NTP_CONF), stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate(('\n'.join(lines) + '\n').encode())
        if p.returncode:
            return 'Nije sačuvano: ' + err.decode(errors='replace').strip()[:120]
        await self._run('sudo', '-n', 'systemctl', 'restart', 'chrony', timeout=20)

        async def later():   # prva sinhronizacija traje par sekundi
            await asyncio.sleep(8)
            await self.ntp_refresh()
            self.mark()
        asyncio.get_running_loop().create_task(later())
        return None

    async def ntp_refresh(self):
        tr = await self._run('chronyc', '-n', 'tracking', timeout=5)
        src = await self._run('chronyc', '-n', 'sources', timeout=5)
        t = dict((k.strip(), v.strip()) for k, v in (l.split(':', 1) for l in tr.splitlines() if ':' in l))
        sources = []
        for ln in src.splitlines():
            m = re.match(r'^\^([*+\-?x~])\s+(\S+)\s+(\d+)\s+\d+\s+(\d+)', ln)
            if m:
                sources.append({'state': m.group(1), 'addr': m.group(2), 'stratum': int(m.group(3)), 'reach': int(m.group(4), 8)})
        try:
            off = float(t.get('System time', '0').split()[0]) * (1 if 'fast' in t.get('System time', '') else -1) * 1000
        except ValueError:
            off = None
        ref = t.get('Reference ID', '')
        clients = 0
        if self.ntp_cfg().get('serve'):
            cl = await self._run('sudo', '-n', 'chronyc', '-n', 'clients', timeout=5)
            clients = sum(1 for l in cl.splitlines()[2:] if l.strip() and not l.startswith('='))
        self.ntp_st = {'sync': t.get('Leap status') == 'Normal' and '()' not in ref and bool(ref), 'ref': (re.search(r'\((.*)\)', ref) or [None, ''])[1],
                       'stratum': t.get('Stratum', ''), 'offset': off, 'sources': sources, 'clients': clients}
        self.net['ntp'] = {'sync': self.ntp_st['sync'], 'server': self.ntp_st['ref'], 'servers': self.ntp_cfg()['servers']}

    def ntp_view(self):
        lan = next(((i.get('ip') or [''])[0].split('/')[0] for i in self.net.get('ifs', []) if i.get('ip') and not i['n'].startswith('tun')), '')
        c = self.ntp_cfg()
        return dict({k: v for k, v in (getattr(self, 'ntp_st', None) or {}).items() if k != 'sources'}, client=bool(c['client']), serve=bool(c.get('serve')), ip=lan,
                    preset=next((k for k, v in NTP_PRESETS.items() if v == c['servers']), 'rs'), installed=bool(shutil.which('chronyc')))

    async def ntp_set(self, a):
        """NTP klijent: uključen / isključen i izvor (Srbija rs.pool.ntp.org ili Cloudflare)."""
        c = self.ntp_cfg()
        if a.get('preset') in NTP_PRESETS:
            c['servers'] = list(NTP_PRESETS[a['preset']])
        if 'client' in a:
            c['client'] = bool(a['client'])
        save_state(self.st)
        err = await self.ntp_write()
        if err:
            return {'err': err}
        self.mark()
        if not c['client']:
            return {'ok': 'NTP klijent isključen — sat Pi-ja ide sam, bez provere'}
        return {'ok': 'Vreme sa: ' + ('Srbija (rs.pool.ntp.org)' if c['servers'] == NTP_PRESETS['rs'] else 'Cloudflare') + ' · sinhronizujem…'}

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
        if self.arc_running():   # igra: mpv je ugašen, jačina ide na zvuk emulatora
            self.arc['vol'], self.arc['mute'] = v, False
            await self.arc_audio()
            self.mark()
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
        if self.arc_running():
            self.arc['mute'] = not self.arc['mute']
            await self.arc_audio()
            self.mark()
            return
        m = not bool(self.mpv.props.get('mute'))
        await self.mpv.cmd('set_property', 'mute', m)
        for sock in HELPER_SOCKS:
            await mpv_oneshot(sock, 'set_property', 'mute', m)

    # ---------- nasumično: stanica, kanal ili ceo izvor
    RAND_MEMORY = {'st': 12, 'tv': 25, 'yt': 10, 'av': 1, 'src': 2, 'all': 2, 'fm': 3, 'arc': 4}   # koliko poslednjih izbora se ne ponavlja

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
        what = what or {'st': 'st', 'tv': 'tv', 'av1': 'yt', 'arc': 'arc'}.get(self.source, 'av' if self.source in AVS else 'src')
        if what == 'arc':   # nasumična igra sa flasha (ne ista koja se upravo igra)
            games = self.arc_games()
            if not games:
                return {'err': 'Nema igara na flashu (folder CD).'}
            a = getattr(self, 'arc', None)
            x = self._rand('arc', games, key=lambda v: v['id'], exclude=(a['game']['id'],) if a and self.arc_running() else ())
            r = await self.arc_start(x['id'])
            return {'ok': 'Nasumično: ' + x['name']} if r.get('ok') else r
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
            what = self._rand('src', [s for s in SOURCES if s not in AVS and s != 'arc'], exclude=(self.source,))   # bez AV ulaza i Arcade
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

    # ---------- IR dekoder (Alati → Elektronika): protokol, adresa, komanda svakog tastera; dok je otvoren, daljinski ne upravlja Teco-om
    IRDEC_SHM = Path('/dev/shm/teco-irdec.json') if Path('/dev/shm').is_dir() else Path('/tmp/teco-irdec.json')

    # ---------- Multimetar (primer): stranica šalje prikaz (simulacija), TV ga crta; kasnije podatke daje BLE most na serveru
    MM_SHM = Path('/dev/shm/teco-mm.json') if Path('/dev/shm').is_dir() else Path('/tmp/teco-mm.json')

    def mm_show(self, a):
        if self.tool_running() != 'mm':
            return {}
        keep = ('conn', 'dev', 'val', 'unit', 'flags', 'acdc', 'bar', 'range', 'minmax', 'fn', 'hist', 'light', 'beep')
        v = {k: a[k] for k in keep if k in a}
        v['hist'] = [float(x) for x in (v.get('hist') or [])[-240:] if isinstance(x, (int, float))]
        try:
            tmp = self.MM_SHM.with_suffix('.tmp')
            tmp.write_text(json.dumps(v))
            os.replace(tmp, self.MM_SHM)
        except OSError:
            pass
        return {}

    def irdec_active(self):
        return time.time() < getattr(self, 'irdec_until', 0)

    def irdec_view(self):
        return {'on': self.irdec_active(), 'hist': getattr(self, 'irdec_hist', [])[:30]}

    async def irdec_set(self, on):
        """Stranica javlja da je dekoder otvoren (na 60 s); bez javljanja se sam gasi posle 2 min."""
        if not on:
            self.irdec_until = 0
            return {}
        self.irdec_until = time.time() + 120
        t = getattr(self, 'irdec_task', None)
        if not t or t.done():
            self.irdec_task = asyncio.get_running_loop().create_task(self.irdec_loop())
        return {}

    async def irdec_loop(self):
        stop = asyncio.Event()

        async def watch():
            while self.irdec_active():
                await asyncio.sleep(2)
            stop.set()
        asyncio.get_running_loop().create_task(watch())
        self.irdec_hist = getattr(self, 'irdec_hist', [])
        try:
            async for proto, sc, rep in hw.ir_scancodes(stop):
                h = self.irdec_hist
                if rep and h and h[0]['sc'] == sc and h[0]['p'] == proto:
                    h[0]['n'] += 1   # držan taster: samo brojač
                else:
                    e = {'t': time.time(), 'p': proto, 'proto': hw.RC_PROTO.get(proto, str(proto)), 'sc': sc, 'hex': '0x%X' % sc, 'n': 1, **hw.ir_split(proto, sc)}
                    h.insert(0, e)
                    del h[50:]
                v = self.irdec_view()
                if self.tool_running() == 'irdec':
                    try:
                        self.IRDEC_SHM.write_text(json.dumps(v))
                    except OSError:
                        pass
                await self.push({'irdec': v})
        except OSError as e:
            log.warning('IR dekoder: %s', e)
            await self.push({'irdec': dict(self.irdec_view(), err=str(e))})

    async def ir_hw(self, code, rep):
        if self.irdec_active():   # dekoder je otvoren: tasteri se samo prikazuju, ne upravljaju Teco-om
            return
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
        if c not in ('ping', 'check', 'vol', 'seek', 'seek_rel', 'vault_get', 'vault_put', 'phone_set', 'sms_send', 'sms_thread', 'sms_read', 'notify_set', 'mm_show', 'note_set'):   # zapis komandi sa stranice (bez PIN-a i tokena; mm_show je 4x u s)
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
            if 'pgm' in a:   # okida ga PGM alarma (kad se uključi), prazno = ne
                ev['pgm'] = int(a['pgm']) if str(a['pgm']).strip() not in ('', '0', 'None') else None
            if a.get('tone') and a['id'] not in ALARMS:   # ime fajla iz Skladište → Zvukovi
                t = str(a['tone'])
                if '/' in t or not (TONES_DIR / t).is_file():
                    return {'err': 'Nema tog zvuka u Zvukovima.'}
                ev['tone'] = t
            save_state(self.st)
            await self.events_assets()
        elif c == 'ev_test':
            return await self.fire_event(a.get('id'))
        elif c == 'tone_play':   # samo melodija (bez slike na ekranu): izbor u Obaveštenjima, ▶ u Zvukovima
            t = str(a.get('f') or '')
            p = TONES_DIR / t
            if not t or '/' in t or not p.is_file():
                return {'err': 'Nema tog zvuka.'}
            if getattr(self, 'tone_proc', None) and self.tone_proc.returncode is None:
                self.tone_proc.terminate()
            self.tone_proc = await self.play_sound(p)
            return {'ok': '▶ ' + next((x['n'] for x in self.tones_list() if x['f'] == t), t)}
        elif c == 'tone_del':
            t = str(a.get('f') or '')
            p = TONES_DIR / t
            if not t or '/' in t or not p.is_file():
                return {'err': 'Nema tog zvuka.'}
            if p.stem in EVENT_TONES and p.suffix == '.wav':
                return {'err': 'Ugrađena melodija se ne briše.'}
            p.unlink()
            for ev in self.st['events'].values():
                if ev.get('tone') == t:
                    ev.pop('tone')
            save_state(self.st)
            self.mark()
            return {'ok': 'Obrisano: ' + t}
        elif c == 'net_set':
            return await self.net_set(a)
        elif c == 'dns_set':
            return await self.dns_set(a)
        elif c == 'net_mode':
            return await self.net_mode(a.get('v'))
        elif c in ('vault_get', 'vault_put'):   # Alati → Lozinke: šifrovan trezor (šifruje se u pregledaču, Pi ne zna lozinke)
            f = DATA / 'vault.json'
            try:
                cur = json.loads(f.read_text())
            except (OSError, ValueError):
                cur = None
            if c == 'vault_get':
                return {'vault': cur}
            b = a.get('vault')
            if not isinstance(b, dict) or not all(isinstance(b.get(k), str) for k in ('salt', 'nonce', 'box')) or len(json.dumps(b)) > 2_000_000:
                return {'err': 'Neispravan trezor.'}
            if cur and int(a.get('base') or 0) != int(cur.get('rev') or 0):
                return {'err': 'Trezor je u međuvremenu promenjen na drugom uređaju — zaključaj i otključaj ponovo.'}
            b = {k: b[k] for k in ('v', 'kdf', 'iter', 'salt', 'nonce', 'box') if k in b}
            b['rev'] = int((cur or {}).get('rev') or 0) + 1
            b['ts'] = int(time.time())
            tmp = f.with_suffix('.tmp')
            tmp.write_text(json.dumps(b))
            os.chmod(tmp, 0o600)
            os.replace(tmp, f)
            return {'ok': True, 'rev': b['rev']}
        elif c in ('tool_set', 'tool_show', 'tool_hide'):   # Alati: osciloskop, otpornici (prikaz na ekranu)
            name = str(a.get('name') or '')
            if c == 'tool_hide':
                return await self.tool_hide()
            if name not in self.TOOL_SCRIPTS:
                return {'err': 'Nepoznat alat.'}
            return await self.tool_show(name) if c == 'tool_show' else self.tool_set(name, a.get('cfg') or {})
        elif c == 'net_test':   # Alati: mrežni tester
            return await self.net_test()
        elif c == 'phone_set':   # Alati: telefonska linija (SLIC + SIM800)
            return self.phone_set(a.get('cfg') or {})
        elif c == 'phone_test':
            return await self.phone_test(a)
        elif c in ('pbx_set', 'pbx_secret', 'pbx_prompt_del', 'pbx_vm_del', 'pbx_vm_heard'):   # Telefonska centrala (Asterisk)
            return await self.pbx_cmd(c, a)
        elif c == 'phone_sim':   # simulacija modema i slušalice (test bez hardvera)
            return await self.phone_sim(a)
        elif c in ('sms_send', 'sms_thread', 'sms_read', 'sms_del', 'sms_name', 'sms_retry'):   # Alati: SMS poruke
            return await self.sms_cmd(c, a)
        elif c == 'notify_set':   # SMS obaveštenja i komande
            return self.notify_set(a.get('cfg') or {})
        elif c == 'ussd':
            return await self.ussd(a.get('code'))
        elif c == 'scan_watch':   # prati uređaj (SMS kad nestane sa mreže)
            d = self.scan_db()['devices'].get(str(a.get('key') or ''))
            if not d:
                return {'err': 'Nepoznat uređaj.'}
            d['watch'] = bool(a.get('on'))
            self.scan_save()
            self.mark()
            return {'ok': 'Uređaj se prati' if d['watch'] else 'Uređaj se više ne prati'}
        elif c in ('dns_lookup', 'whois'):   # Alati: DNS (nslookup) i whois
            import dnstools
            q = dnstools.valid_name(str(a.get('q') or ''))
            if not q:
                return {'err': 'Upiši ispravno ime domena ili IP adresu.'}
            loop = asyncio.get_running_loop()
            try:
                if c == 'whois':
                    return {'whois': await asyncio.wait_for(loop.run_in_executor(None, dnstools.whois, q), 30)}
                t = str(a.get('type') or 'ALL').upper()
                if t != 'ALL' and t not in dnstools.TYPES:
                    return {'err': 'Nepoznat tip zapisa.'}
                srv = str(a.get('server') or 'system')
                if srv == 'system':
                    srv = ((self.net.get('dns') or {}).get('active') or ['8.8.8.8'])[0]
                elif srv == 'router':
                    srv = self.net.get('gw') or '8.8.8.8'
                ipaddress.ip_address(srv)
                return {'dns': await asyncio.wait_for(loop.run_in_executor(None, dnstools.lookup, q, t, srv), 30)}
            except ValueError:
                return {'err': 'Neispravna adresa DNS servera.'}
            except (OSError, asyncio.TimeoutError) as e:
                return {'err': 'Nema odgovora: %s' % (e or 'isteklo vreme')}
        elif c == 'scan_run':   # Alati: skeniranje mreže
            return await self.scan_run(a.get('ifs'))
        elif c == 'wol':   # Wake-on-LAN: "magični paket" za uređaj iz spiska
            return await self.wol(str(a.get('key') or ''))
        elif c == 'logic_pk2':   # Logički analizator: snimak sa PICkit 2
            return await self.la_pk2_capture()
        elif c == 'logic_pk2_stop':
            ev = getattr(self, 'la_pk2_cancel', None)
            if ev:
                ev.set()
            return {}
        elif c == 'logic_pk2_get':   # poslednji snimak (uzorci) za prikaz na stranici
            try:
                return {'cap': json.loads(self.LA_PK2_FILE.read_text())}
            except (OSError, ValueError):
                return {'cap': None}
        elif c in ('prog_list', 'prog_esp', 'prog_avr', 'prog_pic', 'prog_stop', 'prog_install', 'prog_save'):   # Alati → ESP flasher / AVR programer
            return await self.prog_cmd(c, a)
        elif c == 'ds_list':   # Alati → Pinout: PDF-ovi (datasheet) sa flash-a
            return await self.ds_list()
        elif c in ('dnsr_on', 'dnsr_set', 'dnsr_host', 'dnsr_clear'):   # Alati → DNS resolver
            return await self.dnsr_cmd(c, a)
        elif c == 'wds_list':   # Alati → WDS: ISO fajlovi na flash-u
            return await self.wds_list()
        elif c == 'wds_install':
            return await self.wds_install()
        elif c == 'wds_start':
            return await self.wds_start(str(a.get('iso') or ''))
        elif c == 'wds_stop':
            return await self.wds_stop()
        elif c == 'scan_add':   # ručno dodat uređaj (npr. ugašen računar za buđenje)
            mac = str(a.get('mac') or '').strip().lower().replace('-', ':')
            if re.fullmatch(r'[0-9a-f]{12}', mac):
                mac = ':'.join(mac[i:i + 2] for i in range(0, 12, 2))
            if not re.fullmatch(r'([0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
                return {'err': 'Neispravna MAC adresa (npr. 00:1a:2b:3c:4d:5e).'}
            if int(mac[1], 16) & 2:
                return {'err': 'To je privatna (nasumična) MAC adresa — buđenje ne radi; upiši pravu MAC mrežne kartice.'}
            import netscan
            db = self.scan_db()
            d = db['devices'].setdefault(mac, {'first': time.time(), 'last': 0, 'ip': '', 'online': False, 'ports': [], 'name': '',
                                               'type': 'pc', 'kind': 'Računar (dodat ručno)', 'ifc': 'wlan0'})
            d.update(mac=mac, label=str(a.get('label') or '').strip()[:40] or d.get('label', ''), vendor=netscan.vendor(mac) if netscan._manuf else d.get('vendor'))
            self.scan_save()
            self.mark()
            return {'ok': 'Uređaj dodat'}
        elif c in ('scan_label', 'scan_forget'):
            db = self.scan_db()
            d = db['devices'].get(str(a.get('key') or ''))
            if not d:
                return {'err': 'Nepoznat uređaj.'}
            if c == 'scan_forget':
                del db['devices'][str(a['key'])]
            else:
                d['label'] = str(a.get('label') or '').strip()[:40]
            self.scan_save()
            if self.tool_running() == 'scan':
                self.tool_write('scan')
            self.mark()
            return {}
        elif c == 'aqua_set':   # akvarijum: željeno stanje izlaza (ESP32 ga preuzme pri sledećem javljanju)
            aq, k = self.aqua_cfg(), str(a.get('k') or '')
            if k not in aq['want']:
                return {'err': 'Nepoznat izlaz.'}
            if 'rgb' in a:   # boja RGB svetla
                if k not in self.AQUA_RGB or not re.fullmatch(r'#[0-9a-fA-F]{6}', str(a['rgb'])):
                    return {'err': 'Neispravna boja.'}
                aq['rgb'][k] = str(a['rgb']).lower()
                save_state(self.st)
                self.mark()
                return {'ok': dict(self.AQUA_OUTS)[k] + ': boja ' + aq['rgb'][k]}
            on = bool(a.get('on'))
            lv = (getattr(self, 'aqua_live', None) or {}).get('levels', {})
            if on and k == 'valve_in' and (lv.get('max') or lv.get('alarm')):
                return {'err': 'Voda je na MAX nivou — dovod se ne otvara.'}
            if on and k == 'pump_out' and lv.get('min') is False:
                return {'err': 'Voda je ispod MIN nivoa — odvod se ne pali.'}
            aq['want'][k] = on
            save_state(self.st)
            self.mark()
            return {'ok': dict(self.AQUA_OUTS)[k] + (': uključeno' if on else ': isključeno')}
        elif c in ('scr_set', 'scr_press'):   # dugme SOURCE na ekranu (Admin)
            return await self.scr_cmd(c, a)
        elif c in ('fav_add', 'fav_del', 'fav_move'):   # Dashboard: omiljene prečice (zajedničke za sve telefone)
            favs = self.st.setdefault('favs', [])
            if c == 'fav_add':
                t, v = str(a.get('t') or ''), str(a.get('v') or '')[:40]
                if t not in ('dev', 'page', 'src') or not v:
                    return {'err': 'Nepoznata prečica.'}
                if any(f['t'] == t and f['v'] == v for f in favs):
                    return {'err': 'Već je na Dashboard-u.'}
                if len(favs) >= 40:
                    return {'err': 'Najviše 40 prečica.'}
                favs.append({'t': t, 'v': v})
            else:
                try:
                    i = int(a.get('i'))
                    if not 0 <= i < len(favs):
                        raise ValueError
                except (TypeError, ValueError):
                    return {'err': 'Prečica nije pronađena.'}
                if c == 'fav_del':
                    favs.pop(i)
                else:
                    j = i + (1 if int(a.get('d') or 0) > 0 else -1)
                    if 0 <= j < len(favs):
                        favs[i], favs[j] = favs[j], favs[i]
            save_state(self.st)
            self.mark()
            return {}
        elif c == 'arc_aspect':   # format slike za igre (važi od sledećeg pokretanja igre)
            if a.get('v') not in ('43', 'wide'):
                return {'err': 'Nepoznat format.'}
            self.st['arc_aspect'] = a['v']
            save_state(self.st)
            self.mark()
            return {'ok': 'Slika: ' + ('preko celog ekrana' if a['v'] == 'wide' else '4:3') + (' · važi od sledećeg pokretanja igre' if self.arc_running() else '')}
        elif c == 'arc_start':
            return await self.arc_start(str(a.get('id') or ''))
        elif c == 'arc_stop':
            await self.arc_stop()
            return {'ok': 'Izlazim iz igre'}
        elif c in ('dev_add', 'dev_edit', 'dev_del', 'dev_set', 'dev_scan'):
            return await self.dev_cmd(c, a)
        elif c == 'smb_share_add':
            return await self.smb_share_add(a)
        elif c == 'smb_share_set':
            return await self.smb_share_set(a)
        elif c == 'smb_share_del':
            return await self.smb_share_del(a)
        elif c == 'alarm_cfg':
            return await self.alarm_cfg_set(a)
        elif c == 'alarm_add':
            return await self.alarm_add(a)
        elif c == 'alarm_del':
            return await self.alarm_del(str(a.get('id') or ''))
        elif c == 'alarm':   # uključi/isključi: dozvoljeno svima u mreži (izbor korisnika)
            return await self.alarm_do(a)
        elif c in ('alarm_monitor', 'alarm_mem_clear', 'alarm_ev_clear', 'alarm_events', 'alarm_time', 'alarm_panic'):
            return await self.alarm_extra(c, a)
        elif c == 'ntp_set':
            return await self.ntp_set(a)
        elif c == 'wifi_an':   # Alati → Wi-Fi analizator
            return await self.wifi_an(a.get('rescan', True) is not False)
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
        elif c == 'smb_user_add':
            return await self.smb_user_add(a.get('user'), a.get('pw'))
        elif c == 'smb_user_del':
            return await self.smb_user_del(str(a.get('user') or ''))
        elif c == 'stor_scan':
            return await self.stor_scan(bool(a.get('force')), a.get('dev'))
        elif c == 'ext_on':
            return await self.ext_on(str(a.get('ext') or ''), bool(a.get('on')))
        elif c == 'bura':
            return await self.bura_cmd(a)
        elif c == 'cups':
            return await self.cups_cmd(a)
        elif c in ('note_add', 'note_set', 'note_del'):
            return self.notes_cmd(c, a)
        elif c == 'mm_show':
            return self.mm_show(a)
        elif c == 'irdec':
            if a.get('clear'):
                self.irdec_hist = []
                if self.tool_running() == 'irdec':
                    self.tool_write('irdec')
                await self.push({'irdec': self.irdec_view()})
                return {}
            return await self.irdec_set(bool(a.get('on')))
        elif c in ('mail_set', 'mail_test'):
            return await self.mail_cmd(c, a)
        elif c in ('web_add', 'web_set', 'web_del', 'web_check'):
            return await self.web_cmd(c, a)
        elif c == 'gen_set':
            return await self.gen_set(a)
        elif c == 'gen_run':
            return await self.gen_run(bool(a.get('v')))
        elif c == 'stor_ls':
            return await self.stor_ls(a)
        elif c == 'stor_mkdir':
            return await self.stor_mkdir(a)
        elif c == 'stor_del':
            return await self.stor_del(a)
        elif c == 'smb_direct':
            return await self.net_direct(bool(a.get('v')))
        elif c == 'smb_ls':
            return await self.smb_ls(a)
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

            def sensors():
                """Senzori se čitaju u posebnoj niti: DHT22 i DS18B20 (1-Wire) čitanje traje 0,3-0,8 s
                i inače blokira server (stranica tada ne odgovara)."""
                try:
                    cpu = round(int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000, 1)
                except (OSError, ValueError):
                    cpu = None
                # unutra: DHT22 (sa vlažnošću) ili DS18B20 na 1-Wire; napolju: vrednost poslata preko /api/outside
                temps = []
                devs = [d for fam in ('10', '22', '28', '3b', '42') for d in glob.glob('/sys/bus/w1/devices/%s-*/temperature' % fam)]
                for dev in sorted(devs):
                    try:
                        temps.append(round(int(Path(dev).read_text()) / 1000, 1))
                    except (OSError, ValueError):
                        temps.append(None)
                # DHT22 preko kernel drajvera (dtoverlay=dht11): temperatura + vlažnost; čitanje ponekad ne uspe
                dt = dh = None
                for dev in glob.glob('/sys/bus/iio/devices/iio:device*'):
                    try:
                        if Path(dev, 'name').read_text().strip().startswith('dht'):
                            dt = round(int(Path(dev, 'in_temp_input').read_text()) / 1000, 1)
                            dh = round(int(Path(dev, 'in_humidityrelative_input').read_text()) / 1000)
                    except (OSError, ValueError):
                        pass
                return cpu, temps, dt, dh
            s['cpu'], temps, dht_t, dht_h = await asyncio.get_running_loop().run_in_executor(None, sensors)
            if dht_t is not None:   # zadnja dobra vrednost se drži do 60 s
                self.dht_last = (dht_t, dht_h, time.monotonic())
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
            'title': p.get('media-title') if self.mode == 'yt' and not self.is_black() else None,
            'pos': 0 if self.is_black() else p.get('time-pos') or 0,
            'dur': 0 if self.is_black() else p.get('duration') or 0,
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
            'theme': self.st.get('theme', 'light'),   # podrazumevano: svetla (IQlogic izgled)
            'wds': self.wds_view(),
            'dnsr': self.dnsr_view(),
            'prog': self.prog_view(),
            'la_pk2': self.la_pk2_view(),
            'gen': self.gen_view(),
            'web': self.web_view(),
            'mail': self.mail_view(),
            'ext_on': self.ext_state(),
            'alarms': [self.alarm_snapshot(x['id']) for x in self.alarm_list()],
            'alarm_panels': self.ALARM_PANELS,   # za formu „Dodaj uređaj → Alarm“
            'alarm': self.alarm_snapshot(self.alarm_list()[0]['id']) if self.alarm_list() else {},   # glavni (prvi) alarm
            'last_av': self.st.get('last_av', 'av2'),
            'radio_clock': int(self.st.get('radio_clock', 15) or 0),
            'yt_cookies': self.yt_cookie_state(),
            'wifi': self.wifi,
            'stream': {'sys': SYS_STREAMS, 'my': self.st['stream']['list'], 'fav': self.st['stream'].get('fav') or [],
                       'cur': (self.stream_cur() or (0,))[0], 'err': self.st_err,
                       'on': self.source == 'st' and self.st_proc is not None, 'title': self.st_title},
            'relays': [{'n': r['n'], 'on': r['on'], 'role': r.get('role')} for r in self.st['relays']],
            'inputs': self.input_state(),
            'io': [   # stanje uređaja (I/O prozor)
                {'n': 'IR prijemnik', 'd': 'TSOP', 'ok': os.path.exists('/dev/lirc0')},
                {'n': 'FM tjuner', 'd': 'RDA5807M · I2C 0x11', 'ok': bool(self.tuner)},
                {'n': 'Pojačalo', 'd': 'TPA2016D2 · I2C 0x58', 'ok': bool(self.amp)},
                {'n': 'Tasteri', 'd': 'PCF8574 · I2C 0x27', 'ok': bool(getattr(self, 'keys_addr', None))},
                {'n': 'Unutra', 'd': 'DHT22', 'ok': self.sys.get('room') is not None},
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
            'tones': self.tones_list(),   # Skladište → Zvukovi (SD)
            'bura': self.bura_view(),     # vaga; uživo stiže i posebnom porukom {'bura': …}
            'cups': self.cups_view(),
            'ntp': self.ntp_view(),
            'notes': self.notes_db(),
            'irdec': self.irdec_view(),
            'ev_now': self.ev_now,
            'cast': {'on': bool(self.st.get('cast')), 'run': bool(getattr(self, 'cast_run', False)),
                     'sender': self.cast_sender},
            'smb': self.smb_snapshot(),
            'devices': self.dev_snapshot(),
            'arcade': self.arc_snapshot(),
            'favs': self.st.get('favs', []),
            'scrbtn': {k: v for k, v in self.scr_cfg().items() if k != 'pin'},
            'aqua': self.aqua_snapshot(),
            'tools': {'timer': dict(self.tool_cfg('timer'), now=time.time()), 'scope': self.tool_cfg('scope'), 'resistor': self.tool_cfg('resistor'), 'logic': self.tool_cfg('logic'), 'capacitor': self.tool_cfg('capacitor'), 'guest': self.tool_cfg('guest'), **self.comp_views(), 'net': self.nt_view(), 'scan': self.scan_view(), 'phone': self.phone_view(), 'sms': self.sms_view(), 'pbx': self.pbx_view(), 'shown': self.tool_running()},
            'dev_drivers': [{'key': d.key, 'label': d.label, 'ready': d.ready, 'type': d.dtype} for d in DEV_DRIVERS.values()],
            'usbcams': UsbCamDriver.list_devices(),   # priključene USB kamere (izbor u Adminu)
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

    page = {}

    async def index(request):
        """Stranica (~800 KB) gzip-ovana jednom po izmeni fajla (~5x manja preko Wi-Fi-ja/VPN-a); ista verzija -> 304."""
        f = STATIC / 'index.html'
        st = f.stat()
        if page.get('key') != (st.st_mtime_ns, st.st_size):
            import gzip
            raw = f.read_bytes()
            page.update(key=(st.st_mtime_ns, st.st_size), raw=raw, gz=gzip.compress(raw, 6),
                        etag='"%x-%x"' % (st.st_mtime_ns, st.st_size))
        h = {'Cache-Control': 'no-cache', 'ETag': page['etag'], 'Vary': 'Accept-Encoding'}
        if request.headers.get('If-None-Match') == page['etag']:
            return web.Response(status=304, headers=h)
        if 'gzip' in request.headers.get('Accept-Encoding', ''):
            return web.Response(body=page['gz'], content_type='text/html', charset='utf-8', headers={**h, 'Content-Encoding': 'gzip'})
        return web.Response(body=page['raw'], content_type='text/html', charset='utf-8', headers=h)

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
        """Zvuk u Skladište → Zvukovi: /api/event-sound[?id=zvono] (sa id-jem odmah postaje melodija tog događaja).
        Isto ime dobija „(2)“, ništa se ne prepisuje."""
        if request.headers.get('X-Teco-Token') not in teco.tokens:
            return web.json_response({'err': 'Potreban je Admin režim.'}, status=403)
        eid = request.query.get('id', '')
        if eid and (eid not in teco.st['events'] or eid in ALARMS):
            return web.json_response({'err': 'Nepoznat događaj.'}, status=400)
        reader = await request.multipart()
        part = await reader.next()
        name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', Path(part.filename or '').name).strip(' .')[:80]
        ext = Path(name).suffix.lower()
        if ext not in AUDIO_EXT:
            return web.json_response({'err': 'Podržani su mp3, wav, ogg, m4a, flac.'}, status=400)
        data = await part.read(decode=False)
        if len(data) > 20 * 2**20:
            return web.json_response({'err': 'Zvuk je veći od 20 MB.'}, status=400)
        TONES_DIR.mkdir(parents=True, exist_ok=True)
        dest = teco.free_name(TONES_DIR / name)
        dest.write_bytes(data)
        if eid:
            teco.st['events'][eid]['tone'] = dest.name
            save_state(teco.st)
        teco.mark()
        return web.json_response({'ok': 'Sačuvano u Zvukovi: ' + dest.name})

    async def upload(request):   # Skladište -> Čuvar ekrana: slike za ekran kad nema videa (SD kartica, images/)
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
        """Fajl na USB disk (Skladište): POST /api/game?dev=/dev/sda1&cat=video&name=film.mkv, telo = fajl.
        Igrice (ISO/ZSO/VCD) idu u OPL foldere game share-a (inače koren diska), ostalo u folder svoje vrste
        (ISOs, Media/Filmovi…). Isto ime: dodaje se (2), (3)… Piše se u delovima u .part fajl, pa se preimenuje kad stigne ceo."""
        dev, cat = request.query.get('dev', ''), request.query.get('cat', 'games')
        sh = next((p for p in getattr(teco, 'usb', []) if p['dev'] == dev and p['mnt']), None)
        if not sh or (cat != 'games' and cat not in teco.STOR_DIRS):
            return web.json_response({'err': 'Disk nije priključen.'}, status=404)
        sh = {**sh, 'n': sh['label'] or sh['n']}
        name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', Path(request.query.get('name', '')).name).strip(' .')[:150]
        ext = Path(name).suffix.lower()
        size = request.content_length
        if not name or not ext and cat != 'data':
            return web.json_response({'err': 'Fajl nema ime ili ekstenziju.'}, status=400)
        if cat == 'games' and ext not in GAME_EXT:
            return web.json_response({'err': '%s: igrice su .iso, .zso (PS2) i .vcd (PS1/POPS).' % name}, status=400)
        kind = teco.STOR_KIND.get(ext, 'data')
        if cat not in ('games', 'data') and kind != cat and not (cat == 'ds' and ext == '.pdf'):
            return web.json_response({'err': '%s nije %s%s.' % (name, teco.STOR_NAMES[cat], ' (to je ' + teco.STOR_NAMES[kind] + ')' if kind != 'data' else '')}, status=400)
        if not size:
            return web.json_response({'err': 'Nepoznata veličina fajla.'}, status=411)
        if sh.get('fs') == 'vfat' and size >= 4 * 2**30:
            return web.json_response({'err': 'FAT32 ne prima fajl od 4 GB i više. Formatiraj disk kao exFAT.'}, status=400)
        if cat == 'games':
            base = teco.stor_base(dev)
            folder = GAME_EXT[ext] or ('CD' if size <= CD_MAX else 'DVD')
        else:   # u folder koji je otvoren u Skladištu (podfolder vrste)
            base, folder = Path(sh['mnt']), teco.STOR_DIRS[cat]
            sub = request.query.get('dir', '').strip('/')
            if sub:
                x = teco.inside(base / folder, sub)
                if not x:
                    return web.json_response({'err': 'Neispravan folder.'}, status=400)
                folder = str(x.relative_to(base.resolve()))
        if shutil.disk_usage(base).free < size + 32 * 2**20:
            return web.json_response({'err': 'Nema dovoljno mesta na %s.' % sh['n']}, status=507)
        try:
            (base / folder).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return web.json_response({'err': 'Folder %s nije napravljen: %s' % (folder, e.strerror)}, status=500)
        dest = teco.free_name(base / folder / name)
        name = dest.name
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

    async def cctv_http(request):
        """/cctv/<id>/<s|m>/<fajl>: HLS kamere; prvi zahtev pokreće ffmpeg, svaki sledeći ga drži živim."""
        dev = next((x for x in teco.st.get('devices', []) if x['id'] == request.match_info['id'] and x.get('type') == 'camera'), None)
        name = request.match_info['name']
        if not dev or not re.fullmatch(r'[\w.-]+\.(m3u8|m4s|mp4|ts)', name):
            raise web.HTTPNotFound()
        s = await teco.cctv_start(dev, request.match_info['q'] != 'm')
        if not s:
            raise web.HTTPNotFound()
        f = s['dir'] / name
        for _ in range(100):   # prvi segment stiže za ~2-5 s
            if f.exists() or s['proc'].returncode is not None:
                break
            await asyncio.sleep(0.1)
        if not f.exists():
            err = (await s['proc'].stderr.read(400)).decode(errors='replace').strip() if s['proc'].returncode is not None else 'kamera ne šalje sliku'
            return web.json_response({'err': err[:300] or 'kamera ne odgovara'}, status=502)
        return web.FileResponse(f, headers={'Cache-Control': 'no-cache'})

    async def mjpeg_http(request):
        """/cctv/<id>/mjpeg: USB kamera uživo (multipart JPEG, za <img>)."""
        dev = next((x for x in teco.st.get('devices', []) if x['id'] == request.match_info['id'] and x.get('proto') == 'usbcam'), None)
        if not dev:
            raise web.HTTPNotFound()
        r = await teco.mjpeg_reader(dev)
        resp = web.StreamResponse(headers={'Content-Type': 'multipart/x-mixed-replace; boundary=frame', 'Cache-Control': 'no-cache',
                                           'X-Accel-Buffering': 'no'})
        await resp.prepare(request)
        r['viewers'] += 1
        last = 0
        try:
            while r['proc'].returncode is None:
                if r['seq'] == last:
                    try:
                        await asyncio.wait_for(r['ev'].wait(), 5)
                    except asyncio.TimeoutError:
                        continue
                last, fr = r['seq'], r['frame']
                if fr:
                    await resp.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n' % len(fr) + fr + b'\r\n')
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            r['viewers'] -= 1
            r['t'] = time.monotonic()
            if r['viewers'] <= 0:   # poslednji gledalac je zatvorio prozor: kamera se gasi posle 1,5 s
                def off():
                    if r['viewers'] <= 0 and r['proc'].returncode is None:
                        r['proc'].kill()
                        log.info('USB kamera %s: ugašena (niko ne gleda)', dev.get('name'))
                asyncio.get_running_loop().call_later(1.5, off)
        return resp

    async def arc_art(request):
        """/arc/art/<id>: omot igre sa flasha (?v=vreme izmene -> pregledač ga sme dugo čuvati)."""
        g = next((x for x in teco.arc_games() if x['id'] == request.match_info['id']), None)
        if not g or not g['art'] or not os.path.isfile(g['art']):
            raise web.HTTPNotFound()
        return web.FileResponse(g['art'], headers={'Cache-Control': 'public, max-age=604800'})

    async def on_start(app):
        # server je restartovan usred igre: igra je ugašena zajedno sa njim, a plejer (mpv) je ostao ugašen
        # i procesor na „performance“ — vrati oba (inače stranica piše „Plejer ne radi“, a ekran je prazan)
        if (await teco._run('pgrep', '-c', 'retroarch', timeout=5)).strip() in ('', '0'):
            if (await teco._run('systemctl', '--user', 'is-active', 'teco-player', timeout=5)).strip() != 'active':
                log.info('plejer je bio ugašen (igra prekinuta restartom) — palim ga')
                await teco._run('systemctl', '--user', 'start', 'teco-player', timeout=15)
            await teco._run('sudo', '-n', 'sh', '-c', 'for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo ondemand > $f; done', timeout=5)
        await teco.events_assets()
        if teco.st.get('cast'):
            await teco.cast_start()
        await teco.smb_refresh(force=True)   # smb.conf i smbd prema podešavanju (i posle instalacije)
        # generator signala posle restarta servera ne radi (stanje se ne pamti): pigpiod bi inače i dalje slao stari talas
        await teco._rc('sudo', '-n', 'systemctl', 'stop', 'pigpiod', timeout=15)
        await teco._rc('sudo', '-n', 'systemctl', 'reset-failed', 'pigpiod', timeout=10)
        for x in teco.alarm_list():   # alarm mostovi (podešeni) — rade i kad se server restartuje
            if teco.alarm_cfg(x['id']).get('enabled'):
                await teco._run('systemctl', '--user', 'start', teco.alarm_unit(x['id']), timeout=20)
        app['tasks'] = [asyncio.create_task(t) for t in (loop_watchdog(), teco.bt_guard_loop(), teco.mpv.run(), teco.sys_loop(), teco.broadcast_loop(), teco.warm_ytdlp(), teco.clock_loop(), teco.slow_loop(), teco.boot_restore(), teco.hw_detect(), teco.ir_loop(), teco.inet_loop(), teco.prerender_consoles(), teco.alarm_loop(), teco.dev_loop(), teco.sms_loop(), teco.notify_loop(), teco.pbx_loop(), teco.wds_loop(), teco.dnsr_loop(), teco.mpv_guard(), teco.timer_loop(), teco.web_loop(), teco.bura_loop(), teco.cups_loop(), teco.notes_loop())]

    async def on_stop(app):
        teco.web_save()   # istorija web monitora
        await teco.fm_stop()
        await teco.stream_stop()   # cast (posebna usluga) ostaje da radi i kad se server restartuje
        for t in app['tasks']:
            t.cancel()

    app.router.add_get('/', index)
    app.router.add_get('/favicon.ico', lambda r: web.FileResponse(STATIC / 'favicon-64.png'))   # pregledači ga traže i bez linka
    app.router.add_get('/ws', ws_handler)

    async def ws_serial(request):   # Alati → Serijski terminal: podaci idu ovim kanalom (binarno, bez snapshot-a)
        import base64
        import serterm
        if not hasattr(teco, 'term'):
            teco.term = serterm.Term()
        term = teco.term
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 2**20)
        await ws.prepare(request)
        await ws.send_str(json.dumps({'st': term.status()}))
        for f in term.history():
            await ws.send_bytes(f)
        term.clients.add(ws)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    a = json.loads(msg.data)
                except ValueError:
                    continue
                c, rep = a.get('c'), {}
                try:
                    if c == 'open':
                        term.open(str(a.get('dev') or ''), a.get('baud') or 115200, str(a.get('fmt') or '8N1'), a.get('dtr', True), a.get('rts', True))
                        log.info('serijski terminal: %s %s %s', term.cfg['dev'], term.cfg['baud'], term.cfg['fmt'])
                    elif c == 'close':
                        term.close()
                    elif c == 'send':
                        term.write(base64.b64decode(a.get('b64') or ''))
                    elif c == 'lines':
                        term.set_lines(a.get('dtr'), a.get('rts'), a.get('brk'))
                    elif c == 'clear':
                        term.hist, term.hist_n = [], 0
                except (ValueError, OSError) as e:
                    rep = {'err': str(e)}
                if c != 'send' or rep:   # slanje: bez stanja posle svakog paketa (brzo), samo greška
                    term._status_all()
                    if rep:
                        await ws.send_str(json.dumps(rep))
        finally:
            term.clients.discard(ws)
        return ws
    app.router.add_get('/ws/serial', ws_serial)

    async def guest_qr(request):   # Alati -> Wi-Fi za goste: QR kod za stranicu (isti kao na ekranu)
        import io
        import wifiqr
        cfg = teco.tool_cfg('guest')
        if not cfg.get('ssid'):
            return web.Response(status=404)
        key = json.dumps(cfg, sort_keys=True)
        c = getattr(teco, 'guest_qr_cache', None)
        if not c or c[0] != key:   # crtanje QR-a je skupo: samo kad se podaci promene
            buf = io.BytesIO()
            wifiqr.image(cfg, 360).save(buf, 'PNG')
            c = teco.guest_qr_cache = (key, buf.getvalue())
        return web.Response(body=c[1], content_type='image/png', headers={'Cache-Control': 'no-store'})
    app.router.add_get('/api/guest-qr.png', guest_qr)

    def app_urls():
        """Adrese aplikacije za telefon: kućna mreža (IP, ne tecopi.local — .local ne radi na svim telefonima/ruterima) i VPN (tun)."""
        lan = vpn = None
        for i in teco.net.get('ifs', []):
            ip = (i.get('ip') or [''])[0].split('/')[0]
            if not ip:
                continue
            if i['n'].startswith('tun'):
                vpn = vpn or ip
            elif i['n'] == 'wlan0' or not lan:
                lan = ip if i['n'] == 'wlan0' or not lan else lan
        return {'lan': 'http://%s/' % lan if lan else None, 'vpn': 'http://%s/' % vpn if vpn else None}

    async def app_qr(request):   # „Aplikacija na telefonu“: QR kod pravi Pi (radi i bez interneta)
        import io
        import wifiqr
        url = app_urls().get(request.query.get('via', 'lan'))
        if not url:
            return web.Response(status=404)
        c = getattr(teco, 'app_qr_cache', {})
        if url not in c:
            buf = io.BytesIO()
            wifiqr.image({}, 320, text=url + '?pwa=1').save(buf, 'PNG')
            c[url] = buf.getvalue()
            teco.app_qr_cache = c
        return web.Response(body=c[url], content_type='image/png', headers={'Cache-Control': 'no-store'})
    app.router.add_get('/api/app-qr.png', app_qr)
    app.router.add_get('/api/app-urls', lambda r: web.json_response(app_urls()))

    async def print_upload(request):   # Štampa (CUPS): fajl sa stranice (PDF, slika, tekst) -> lp na izabrani štampač
        name = re.sub(r'[^A-Za-z0-9_-]', '', request.query.get('printer', ''))
        if not name or not teco.cups_cfg().get('on'):
            return web.json_response({'err': 'Štampa nije uključena ili štampač nije izabran.'}, status=400)
        reader = await request.multipart()
        part = await reader.next()
        fname = Path(part.filename or 'dokument').name
        ext = Path(fname).suffix.lower()
        if ext not in ('.pdf', '.jpg', '.jpeg', '.png', '.gif', '.tif', '.tiff', '.txt', '.ps'):
            return web.json_response({'err': 'Štampa: PDF, slike (JPG/PNG) i tekst (.txt).'}, status=400)
        data = await part.read(decode=False)
        if len(data) > 40 * 2**20:
            return web.json_response({'err': 'Fajl je veći od 40 MB.'}, status=400)
        tmp = Path('/tmp') / ('teco-print-%s%s' % (secrets.token_hex(4), ext))
        tmp.write_bytes(data)
        try:
            args = ['lp', '-d', name, '-t', fname[:60], '-n', str(max(1, min(50, int(request.query.get('n') or 1))))]
            if request.query.get('fit'):
                args += ['-o', 'fit-to-page']
            rc, out = await teco._rc(*args, str(tmp), timeout=60)
        finally:
            asyncio.get_running_loop().call_later(120, lambda: tmp.unlink(missing_ok=True))   # lp kopira u red; čisti se kasnije
        await teco.cups_refresh()
        if rc:
            return web.json_response({'err': (out or 'Nije poslato.').strip().splitlines()[-1][:160]})
        return web.json_response({'ok': 'Poslato na štampu: ' + fname})
    app.router.add_post('/api/print', print_upload)

    async def wds_file(request):   # WDS: iPXE vuče wimboot, BCD, boot.sdi i boot.wim preko HTTP-a (brže od TFTP-a)
        rel, peer = request.match_info['path'], request.remote
        w = getattr(teco, 'wds_rt', None) or {}
        if rel == 'winpeshl.ini':
            return web.Response(text='[LaunchApps]\r\n"install.bat"\r\n')
        if rel == 'install.bat':   # WinPE: mreža, \\<ip>\WDS kao Z:, pa Windows Setup sa mreže
            lan = teco.wds_lan()
            ip = lan[1] if lan else request.host.split(':')[0]
            teco.wds_note('%s: WinPE pokrenut, povezuje se na \\\\%s\\WDS' % (peer, ip))
            bat = ['@echo off', 'wpeinit', 'echo.', 'echo  Teco.Pi WDS: povezujem \\\\%s\\WDS ...' % ip, 'set n=0', ':retry',
                   'net use Z: \\\\%s\\WDS /user:TECOPI\\wds wds >nul 2>&1 && goto ok' % ip, 'set /a n+=1', 'if %n% geq 20 goto fail',
                   'ping -n 4 127.0.0.1 >nul', 'goto retry', ':ok', 'Z:\\setup.exe', 'exit', ':fail',
                   'echo  Mreza nije dostupna (WinPE nema drajver za mreznu karticu?).', 'cmd']
            return web.Response(text='\r\n'.join(bat) + '\r\n')
        if rel == 'wimboot':
            f = teco.WDS_BOOT / 'wimboot'
        elif rel.startswith('iso/') and w.get('mounted'):
            f = teco.wds_find(str(teco.WDS_MNT), rel[4:])
            f = Path(f) if f else None
        else:
            f = None
        if not f or not f.is_file():
            return web.Response(status=404)
        teco.wds_note('%s preuzima %s' % (peer, f.name))
        return web.FileResponse(f)
    app.router.add_get('/wds/{path:.+}', wds_file)

    async def ds_file(request):   # Pinout: datasheet (PDF sa flash-a) po broju iz poslednjeg spiska
        try:
            x = (getattr(teco, 'ds_files', None) or [])[int(request.match_info['i'])]
        except (ValueError, IndexError):
            return web.Response(status=404)
        f = Path(x['path'])
        if not str(f.resolve()).startswith(str(USB_MNT) + '/') or not f.is_file():
            return web.Response(status=404)
        return web.FileResponse(f, headers={'Content-Type': 'application/pdf', 'Content-Disposition': 'inline'})
    app.router.add_get('/api/ds/{i}/{name}', ds_file)

    async def fw_upload(request):   # programatori: firmware (.bin / .hex) sa telefona/računara -> data/fw
        reader = await request.multipart()
        part = await reader.next()
        if part is None or not part.filename:
            return web.json_response({'err': 'Nema fajla.'})
        name = re.sub(r'[^\w.\-]+', '_', os.path.basename(part.filename))[:80]
        if not name.lower().endswith(('.bin', '.hex')):
            return web.json_response({'err': 'Samo .bin ili .hex fajl.'})
        teco.FW_DIR.mkdir(parents=True, exist_ok=True)
        tmp, size = teco.FW_DIR / (name + '.part'), 0
        with open(tmp, 'wb') as f:
            while True:
                ch = await part.read_chunk(65536)
                if not ch:
                    break
                size += len(ch)
                if size > 16 * 2**20:
                    f.close()
                    tmp.unlink(missing_ok=True)
                    return web.json_response({'err': 'Fajl je veći od 16 MB.'})
                f.write(ch)
        os.replace(tmp, teco.FW_DIR / name)
        old = sorted(teco.FW_DIR.glob('*'), key=lambda x: -x.stat().st_mtime)[20:]   # čuva poslednjih 20
        for x in old:
            x.unlink(missing_ok=True)
        r = await teco.prog_list()
        r['sel'] = next((x['i'] for x in r['files'] if x['n'] == name and x['src'] == 'poslato'), None)
        return web.json_response(r)
    app.router.add_post('/api/fw-upload', fw_upload)

    async def pbx_prompt_up(request):   # Centrala: najava (bilo koji zvuk sa telefona / snimak) -> 8 kHz mono WAV za Asterisk
        import pbx
        name = request.match_info['name']
        if name not in pbx.PROMPTS:
            return web.json_response({'err': 'Nepoznata najava.'}, status=404)
        raw = await request.read()
        if not raw or len(raw) > 20 * 2**20:
            return web.json_response({'err': 'Fajl je prazan ili veći od 20 MB.'}, status=400)
        src = DATA / ('prompt-%s.in' % name)
        src.write_bytes(raw)
        dst = pbx.SOUNDS / (name + '.wav')
        out = await teco._run('ffmpeg', '-y', '-v', 'error', '-i', str(src), '-ac', '1', '-ar', '8000', '-c:a', 'pcm_s16le', '-af', 'loudnorm=I=-18:TP=-2',
                              '-t', '120', str(dst), timeout=60)
        src.unlink(missing_ok=True)
        if not dst.exists():
            return web.json_response({'err': 'Zvuk nije prepoznat: ' + out.strip()[:120]}, status=400)
        os.chmod(dst, 0o664)
        teco.mark()
        return web.json_response({'ok': 'Najava sačuvana'})

    async def pbx_wav(request):   # preslušavanje najave ili govorne poruke na stranici
        import pbx
        kind = request.match_info['kind']
        if kind == 'prompt':
            name = request.match_info['a']
            f = pbx.SOUNDS / (name + '.wav') if name in pbx.PROMPTS else None
        else:
            box, folder, mid = request.match_info['a'], request.match_info['b'], request.match_info['c']
            ok = re.fullmatch(r'\d{2,6}', box) and folder in ('INBOX', 'Old', 'Urgent') and re.fullmatch(r'msg\d{4}', mid)
            f = pbx.VM_DIR / box / folder / (mid + '.wav') if ok else None
        if not f or not f.exists():
            return web.Response(status=404)
        return web.FileResponse(f, headers={'Cache-Control': 'no-store'})
    app.router.add_post('/api/pbx-prompt/{name}', pbx_prompt_up)
    app.router.add_get('/api/pbx/{kind}/{a}.wav', pbx_wav)
    app.router.add_get('/api/pbx/{kind}/{a}/{b}/{c}.wav', pbx_wav)
    app.router.add_post('/api/upload', upload)
    app.router.add_post('/api/game', game_upload)
    app.router.add_get('/cctv/{id}/mjpeg', mjpeg_http)
    app.router.add_get('/cctv/{id}/{q}/{name}', cctv_http)
    app.router.add_get('/arc/art/{id}', arc_art)
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

    async def api_aqua(request):
        """ESP32 akvarijuma (Teco ESP protokol, samo lokalna mreža). Na 1-2 s:
        POST /api/aqua  {"id": "aqua1", "ph": 7.1, "temp": 25.4,
                         "levels": {"min": true, "q34": true, "max": false, "alarm": false},
                         "outs": {"pump1": true, "air": true, "heater1": false, "light1": true, "light2": false, "fan": false, "valve_in": false, "pump_out": false}}
        odgovor: {"ok": true, "outs": {...željeno stanje...}, "rgb": {"light2": [r, g, b]}} — ESP postavi releje po "outs",
        a RGB traku (light2) na boju iz "rgb" kad je uključena.
        levels: true = senzor u vodi. GET /api/aqua samo vraća željeno stanje."""
        import ipaddress
        if not ipaddress.ip_address(request.remote).is_private:
            return web.json_response({'err': 'Samo iz lokalne mreže.'}, status=403)
        if request.method == 'GET':
            return web.json_response({'ok': True, 'outs': dict(teco.aqua_cfg()['want']), 'rgb': teco.aqua_rgb_list()})
        try:
            q = await request.json()
            if not isinstance(q, dict):
                raise ValueError
        except ValueError:
            return web.json_response({'err': 'Pošalji JSON (vidi /api/aqua opis u server.py).'}, status=400)
        return web.json_response(teco.aqua_report(q))

    app.router.add_get('/api/aqua', api_aqua)
    app.router.add_post('/api/aqua', api_aqua)
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
    import mimetypes
    mimetypes.add_type('application/manifest+json', '.webmanifest')   # PWA (prečica na telefonu)
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
