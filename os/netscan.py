"""Teco.Pi skeniranje lokalne mreže: ko je na mreži (ping + ARP), imena (ruter DNS, mDNS, NetBIOS),
otvoreni poznati TCP portovi i procena vrste uređaja (telefon, kamera, TV, štampač…).
Samo sopstvene mreže na interfejsima Pi-ja (najviše /24 oko adrese Pi-ja)."""
import asyncio
import ipaddress
import random
import re
import socket
import struct

PORTS = {21: 'FTP', 22: 'SSH', 23: 'Telnet', 53: 'DNS', 80: 'HTTP', 139: 'NetBIOS', 443: 'HTTPS', 445: 'SMB', 515: 'LPD',
         548: 'AFP', 554: 'RTSP', 631: 'IPP', 1883: 'MQTT', 2020: 'ONVIF', 3000: 'Web/LG TV', 3001: 'LG TV', 3389: 'RDP',
         5000: 'Web/NAS', 5001: 'NAS', 5555: 'ADB', 5900: 'VNC', 6053: 'ESPHome', 6668: 'Tuya', 7000: 'AirPlay', 8000: 'HTTP',
         8001: 'Samsung TV', 8008: 'Cast', 8009: 'Cast', 8080: 'HTTP', 8123: 'Home Assistant', 8443: 'HTTPS', 8554: 'RTSP',
         8883: 'MQTTS', 8899: 'ONVIF', 9000: 'Web', 9100: 'Štampač', 9999: 'TP-Link', 32400: 'Plex', 34567: 'DVR',
         37777: 'Dahua', 49152: 'UPnP', 62078: 'iPhone'}

# proizvođač po prva 3 bajta MAC adrese (najčešći kod kuće; nije potpuna baza)
OUI = {}
for vendor, pref in (
        ('Raspberry Pi', 'B827EB DCA632 E45F01 D83ADD 2CCF67'),
        ('Espressif (ESP)', '240AC4 30AEA4 84CCA8 A4CF12 246F28 3C71BF 8CAAB5 BCDDC2 C44F33 ECFABC 600194 98F4AB 483FDA 7C9EBD '
                            'CC50E3 E8DB84 083AF2 349454 40F520 A848FA C8C9A3 10521C 58BF25 94B97E 3C6105 AC67B2 84F3EB 2CF432 '
                            '5CCF7F 18FE34 68C63A'),
        ('Hikvision', '4419B6 BCAD28 C056E3 4CBD8F 2857BE 54C415 A41437 1868CB 8CE748'),
        ('Dahua', '3CEF8C 9002A9 E0508B 4C11BF A0BD1D 14A78B'),
        ('Google', 'F4F5D8 F4F5E8 546009 3C5AB4 D86C63 1CF29A 48D6D5'),
        ('Amazon', '44650D F0272D 74C246 6854FD FC65DE 84D6D0 0C47C9 50DCE7'),
        ('Apple', 'F01898 ACBC32 3C22FB A483E7 F40F24 28CFE9 705681 8C8590 DCA904 9801A7 BCD074 147DDA 60F81D 88665A 4098AD '
                  '7CD1C3 D0817A A85C2C F84D89 90B0ED'),
        ('Samsung', '8C7712 5C0A5B F47B5E 34145F 78BDBC A0CBFD CC07AB 508569 842519 001632'),
        ('TP-Link', '50C7BF 6032B1 98DAC4 B04E26 1C3BF3 C006C3 F4F26D 14CC20 AC84C6 54AF97 30DE4B 68FF7B 5CA6E6'),
        ('Xiaomi', '640980 286C07 7811DC 34CE00 50EC50 7C49EB 04CF8C F8A2D6'),
        ('Sony', 'FC0FE6 001D0D 0019C5 709E29'),
        ('LG', 'A823FE 64BC0C CC2D8C 785DC8')):
    for p in pref.split():
        OUI[p] = vendor


MANUF_URL = 'https://www.wireshark.org/download/automated/data/manuf'
_manuf = None   # {(bitova, hex prefiks): proizvođač} iz Wireshark baze (data/manuf)
_TRIM = re.compile(r'[,.]?\s*\b(co\.?,?\s*ltd\.?|corporation|corporate|inc\.?|ltd\.?|limited|gmbh|s\.?a\.?|a\.?s\.?|b\.?v\.?|llc|ag|oy|ab|'
                   r'technologies|technology|electronics?|communications?|company|co\.?)\b\.?', re.I)


def load_manuf(path):
    """Wireshark manuf: 'AA:BB:CC<tab>kratko<tab>puno ime' ili 'AA:BB:CC:D0:00/28 …'."""
    global _manuf
    db = {}
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                if not line or line[0] == '#':
                    continue
                p = line.rstrip('\n').split('\t')
                if len(p) < 2:
                    continue
                pre, bits = p[0].strip(), 24
                if '/' in pre:
                    pre, b = pre.split('/')
                    bits = int(b)
                hx = pre.replace(':', '').replace('-', '').upper()[:bits // 4]
                name = (p[2] if len(p) > 2 and p[2].strip() else p[1]).strip()
                db[(bits, hx)] = name
    except OSError:
        return False
    _manuf = db
    return True


def short_vendor(name):
    n = _TRIM.sub('', name)
    n = _TRIM.sub('', n).strip(' ,.-')   # dvaput: "Huawei Technologies Co.,Ltd" -> "Huawei"
    return n[:28] or name[:28]


def vendor(mac):
    m = (mac or '').upper().replace(':', '')
    if len(m) < 6:
        return None
    if int(m[1], 16) & 2:   # lokalno dodeljena (nasumična) MAC: telefoni / tableti sa "privatnom adresom"
        return 'privatna MAC'
    if _manuf:
        for bits in (36, 28, 24):
            v = _manuf.get((bits, m[:bits // 4]))
            if v:
                return short_vendor(v)
    return OUI.get(m[:6])


def link_guess(d):
    """Kako je uređaj povezan. Pi vidi samo svoju mrežu; Wi-Fi vs kabl na ruteru se pogađa po vrsti uređaja."""
    ifc, v, t = d.get('ifc'), (d.get('vendor') or '').lower(), d.get('type')
    if ifc == 'eth0':
        return 'kabl'
    if ifc == 'tun0':
        return 'VPN'
    if t == 'pi':
        return {'wlan0': 'Wi-Fi'}.get(ifc, ifc or '')
    if v == 'privatna mac' or t == 'phone' or any(x in v for x in ('espressif', 'wiz', 'tuya', 'xiaomi', 'murata', 'azurewave', 'shenzhen')):
        return 'Wi-Fi'
    if t == 'router':
        return 'ruter'
    return 'Wi-Fi ili kabl'


def guess(d, gw, me):
    """(vrsta, opis) iz portova, proizvođača i imena."""
    p, n = set(d.get('ports') or []), (d.get('name') or '').lower()
    vl = (d.get('vendor') or '').lower()
    has = lambda *xs: any(x in vl for x in xs)
    # kratka imena za stara pravila ispod
    v = next((k for k, xs in (('Hikvision', ('hikvision',)), ('Dahua', ('dahua',)), ('Samsung', ('samsung',)), ('LG', ('lg electronics', 'lg innotek')),
                              ('Sony', ('sony',)), ('Google', ('google',)), ('Apple', ('apple',)), ('Xiaomi', ('xiaomi',)), ('Amazon', ('amazon',)),
                              ('Raspberry Pi', ('raspberry',)), ('Espressif (ESP)', ('espressif',)), ('privatna MAC', ('privatna mac',))) if has(*xs)), d.get('vendor') or '')
    if d['ip'] == me:
        return 'pi', 'Teco.Pi (ovaj uređaj)'
    if d['ip'] == gw:
        return 'router', 'Ruter'
    if has('routerboard', 'mikrotik', 'ubiquiti', 'netgear', 'zyxel', 'cisco', 'aruba', 'd-link') and not p & {554}:
        return 'router', 'Mrežni uređaj (ruter / AP / svič)'
    if has('paradox', 'ajax systems', 'dsc ', 'texecom', 'risco', 'satel'):
        return 'alarm', 'Alarmna centrala'
    if has('wiz'):
        return 'light', 'Pametna sijalica (WiZ)'
    if p & {554, 8554, 37777, 34567, 8899, 2020} or v in ('Hikvision', 'Dahua') or 'cam' in n or 'ipc' in n:
        return 'camera', 'Kamera / DVR'
    if p & {8008, 8009} or p & {8001, 3000, 3001} and v in ('Samsung', 'LG', 'Sony', 'Google') or 'tv' in n or 'chromecast' in n:
        return 'tv', 'TV / Cast'
    if p & {9100, 631, 515} or 'print' in n or 'epson' in n or 'canon' in n or 'hp' == n[:2]:
        return 'printer', 'Štampač'
    if 62078 in p or 'iphone' in n or 'ipad' in n:
        return 'phone', 'iPhone / iPad'
    if 'android' in n or 'galaxy' in n or 'redmi' in n or 'xiaomi' in n or 'pixel' in n or 'phone' in n:
        return 'phone', 'Telefon'
    if 8123 in p:
        return 'server', 'Home Assistant'
    if p & {5000, 5001} and p & {139, 445}:
        return 'server', 'NAS'
    if p & {3389, 445, 139} or 'desktop' in n or 'laptop' in n or 'pc' in n[:3]:
        return 'pc', 'Računar'
    if has('intel', 'lcfc', 'lenovo', 'dell', 'hewlett', 'asustek', 'micro-star', 'gigabyte', 'liteon', 'azurewave', 'realtek') and not p:
        return 'pc', 'Računar / laptop'
    if v == 'Raspberry Pi':
        return 'server', 'Raspberry Pi'
    if v == 'Espressif (ESP)' or p & {6053, 6668, 9999} or v == 'Amazon':
        return 'iot', 'Pametni uređaj' + (' (Alexa / Echo)' if v == 'Amazon' else '')
    if v == 'privatna MAC' and not p:
        return 'phone', 'Telefon / tablet (privatna MAC)'
    if v in ('Samsung', 'Xiaomi', 'Apple') and not p:
        return 'phone', 'Telefon / tablet'
    if 22 in p and not (p - {22}):
        return 'server', 'Linux uređaj (SSH)'
    if p & {80, 443, 8080}:
        return 'web', 'Uređaj sa web stranom'
    return 'unknown', 'Nepoznat uređaj'


def networks(ifs):
    """Mreže za skeniranje: /24 (ili manja) oko adrese Pi-ja na svakom izabranom interfejsu."""
    out = []
    for i in ifs:
        for a in i.get('ip') or []:
            n = ipaddress.ip_interface(a)
            net = n.network if n.network.prefixlen >= 24 else ipaddress.ip_network('%s/24' % n.ip, strict=False)
            out.append((i['n'], str(n.ip), net))
    return out


# ---------- imena: DNS PTR (ruter), mDNS (telefoni, Apple, Android, štampači), NetBIOS (Windows)
def _qname(name):
    return b''.join(bytes([len(x)]) + x.encode() for x in name.split('.')) + b'\0'


def _rdname(buf, i, depth=0):
    labels = []
    while i < len(buf) and depth < 20:
        ln = buf[i]
        if ln == 0:
            return '.'.join(labels), i + 1
        if ln & 0xC0 == 0xC0:
            ptr = struct.unpack('!H', buf[i:i + 2])[0] & 0x3FFF
            name, _ = _rdname(buf, ptr, depth + 1)
            labels.append(name)
            return '.'.join(x for x in labels if x), i + 2
        labels.append(buf[i + 1:i + 1 + ln].decode('utf-8', 'replace'))
        i += 1 + ln
    return '.'.join(labels), i


def ptr_query(server, port, ip, timeout=0.8, mdns=False):
    rev = '.'.join(reversed(ip.split('.'))) + '.in-addr.arpa'
    tid = random.randint(0, 0xFFFF)
    q = struct.pack('!HHHHHH', tid, 0 if mdns else 0x0100, 1, 0, 0, 0) + _qname(rev) + struct.pack('!HH', 12, 0x8001 if mdns else 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, port))
        buf = s.recv(1500)
        an = struct.unpack('!H', buf[6:8])[0]
        if not an:
            return None
        i = 12
        for _ in range(struct.unpack('!H', buf[4:6])[0]):   # preskoči pitanja
            _, i = _rdname(buf, i)
            i += 4
        for _ in range(an):
            _, i = _rdname(buf, i)
            typ, _, _, rdlen = struct.unpack('!HHIH', buf[i:i + 10])
            i += 10
            if typ == 12:
                name, _ = _rdname(buf, i)
                return name.rstrip('.').removesuffix('.local').removesuffix('.lan').removesuffix('.home') or None
            i += rdlen
    except (OSError, struct.error, IndexError):
        return None
    finally:
        s.close()
    return None


def nbns_query(ip, timeout=0.8):
    enc = b''.join(bytes([0x41 + (c >> 4), 0x41 + (c & 15)]) for c in b'*' + b'\0' * 15)
    q = struct.pack('!HHHHHH', random.randint(0, 0xFFFF), 0, 1, 0, 0, 0) + b'\x20' + enc + b'\0' + struct.pack('!HH', 0x21, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (ip, 137))
        buf = s.recv(1500)
        n, i = buf[56], 57
        for _ in range(n):
            name, suf, fl = buf[i:i + 15], buf[i + 15], struct.unpack('!H', buf[i + 16:i + 18])[0]
            i += 18
            if suf == 0 and not fl & 0x8000:
                return name.decode('latin-1').strip() or None
    except (OSError, struct.error, IndexError):
        return None
    finally:
        s.close()
    return None


def lookup_name(ip, gw):
    return (ptr_query(gw, 53, ip) if gw else None) or ptr_query(ip, 5353, ip, mdns=True) or nbns_query(ip)


async def ping(ip, sem):
    async with sem:
        p = await asyncio.create_subprocess_exec('ping', '-n', '-c', '1', '-W', '1', ip,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await p.wait() == 0


async def port_open(ip, port, sem, timeout=0.7):
    async with sem:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
            w.close()
            return True
        except (OSError, asyncio.TimeoutError):
            return False
