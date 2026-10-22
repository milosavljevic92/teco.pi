"""Teco.Pi Alati -> DNS / Whois: nslookup (sopstveni DNS klijent, UDP + TCP kad je odgovor prevelik)
i whois (IANA -> registar -> registrar). Bez spoljnih programa; pozivi su blokirajući (iz niti)."""
import ipaddress
import random
import re
import socket
import struct
import time

TYPES = {'A': 1, 'NS': 2, 'CNAME': 5, 'SOA': 6, 'PTR': 12, 'MX': 15, 'TXT': 16, 'AAAA': 28, 'SRV': 33, 'CAA': 257}
TNAME = {v: k for k, v in TYPES.items()}
ALL = ('A', 'AAAA', 'CNAME', 'MX', 'NS', 'TXT', 'SOA', 'CAA')
RCODE = {0: 'OK', 1: 'neispravan upit', 2: 'greška servera', 3: 'ime ne postoji (NXDOMAIN)', 4: 'nije podržano', 5: 'odbijeno'}
NAME_RE = re.compile(r'^(?=.{1,253}$)([A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9_-]{1,63}\.?$')


def valid_name(s):
    s = (s or '').strip().rstrip('.')
    try:
        ipaddress.ip_address(s)
        return s
    except ValueError:
        pass
    try:
        s = s.encode('idna').decode()   # ćirilica / šđčć domeni
    except UnicodeError:
        return None
    return s if NAME_RE.match(s) else None


def _qname(name):
    return b''.join(bytes([len(x)]) + x.encode() for x in name.rstrip('.').split('.') if x) + b'\0'


def _name(buf, i, depth=0):
    labels = []
    while i < len(buf) and depth < 30:
        ln = buf[i]
        if ln == 0:
            return '.'.join(labels), i + 1
        if ln & 0xC0 == 0xC0:
            ptr = struct.unpack('!H', buf[i:i + 2])[0] & 0x3FFF
            rest, _ = _name(buf, ptr, depth + 1)
            labels.append(rest)
            return '.'.join(x for x in labels if x), i + 2
        labels.append(buf[i + 1:i + 1 + ln].decode('utf-8', 'replace'))
        i += 1 + ln
    return '.'.join(labels), i


def _rdata(buf, i, typ, ln):
    r = buf[i:i + ln]
    if typ == 1:
        return socket.inet_ntoa(r)
    if typ == 28:
        return socket.inet_ntop(socket.AF_INET6, r)
    if typ in (2, 5, 12):
        return _name(buf, i)[0]
    if typ == 15:
        return '%d %s' % (struct.unpack('!H', r[:2])[0], _name(buf, i + 2)[0])
    if typ == 16:
        out, j = [], 0
        while j < len(r):
            n = r[j]
            out.append(r[j + 1:j + 1 + n].decode('utf-8', 'replace'))
            j += 1 + n
        return ''.join(out)
    if typ == 6:
        mname, j = _name(buf, i)
        rname, j = _name(buf, j)
        serial, refresh, retry, expire, minimum = struct.unpack('!IIIII', buf[j:j + 20])
        return '%s %s serial %d (refresh %d, retry %d, expire %d, min %d)' % (mname, rname, serial, refresh, retry, expire, minimum)
    if typ == 33:
        pr, w, port = struct.unpack('!HHH', r[:6])
        return '%d %d %d %s' % (pr, w, port, _name(buf, i + 6)[0])
    if typ == 257:
        tl = r[1]
        return '%d %s "%s"' % (r[0], r[2:2 + tl].decode(errors='replace'), r[2 + tl:].decode(errors='replace'))
    return r.hex()


def query(name, qtype='A', server='8.8.8.8', timeout=3.0):
    """Jedan DNS upit. -> dict(rcode, ms, answers[], authority[], tcp)"""
    qt = TYPES[qtype]
    if qtype == 'PTR':
        try:
            name = ipaddress.ip_address(name).reverse_pointer
        except ValueError:
            pass
    tid = random.randint(0, 0xFFFF)
    opt = b'\0' + struct.pack('!HHIH', 41, 4096, 0, 0)   # EDNS0: odgovor do 4096 B preko UDP-a
    q = struct.pack('!HHHHHH', tid, 0x0100, 1, 0, 0, 1) + _qname(name) + struct.pack('!HH', qt, 1) + opt
    t0 = time.time()
    s = socket.socket(socket.AF_INET6 if ':' in server else socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, 53))
        while True:
            buf = s.recv(4096)
            if buf[:2] == q[:2]:
                break
    finally:
        s.close()
    tcp = False
    if struct.unpack('!H', buf[2:4])[0] & 0x0200:   # TC: odgovor skraćen -> ponovo preko TCP-a
        with socket.create_connection((server, 53), timeout) as c:
            c.sendall(struct.pack('!H', len(q)) + q)
            ln = struct.unpack('!H', _recvn(c, 2))[0]
            buf = _recvn(c, ln)
        tcp = True
    ms = (time.time() - t0) * 1000
    fl, qd, an, ns = struct.unpack('!HHHH', buf[2:10])
    i = 12
    for _ in range(qd):
        _, i = _name(buf, i)
        i += 4
    secs = []
    for cnt in (an, ns):
        rows = []
        for _ in range(cnt):
            nm, i = _name(buf, i)
            typ, _, ttl, ln = struct.unpack('!HHIH', buf[i:i + 10])
            i += 10
            if typ != 41:
                rows.append({'name': nm, 'type': TNAME.get(typ, str(typ)), 'ttl': ttl, 'data': _rdata(buf, i, typ, ln)})
            i += ln
        secs.append(rows)
    return {'rcode': RCODE.get(fl & 0xF, str(fl & 0xF)), 'ms': round(ms, 1), 'answers': secs[0], 'authority': secs[1], 'tcp': tcp}


def _recvn(c, n):
    b = b''
    while len(b) < n:
        x = c.recv(n - len(b))
        if not x:
            break
        b += x
    return b


def lookup(name, qtype, server):
    """nslookup: jedan tip ili ALL (svi uobičajeni). -> dict za stranicu."""
    types = ALL if qtype == 'ALL' else (qtype,)
    if qtype == 'ALL':
        try:
            ipaddress.ip_address(name)
            types = ('PTR',)
        except ValueError:
            pass
    out = {'name': name, 'server': server, 'records': [], 'errors': [], 'ms': None, 'rcode': 'OK'}
    seen = set()
    for t in types:
        try:
            r = query(name, t, server)
        except (OSError, struct.error, IndexError) as e:
            out['errors'].append('%s: %s' % (t, 'nema odgovora' if isinstance(e, socket.timeout) else e))
            continue
        out['ms'] = r['ms'] if out['ms'] is None else max(out['ms'], r['ms'])
        if r['rcode'] != 'OK':
            out['rcode'] = r['rcode']
        for rec in r['answers'] + ([x for x in r['authority'] if x['type'] == 'SOA'] if t == 'SOA' and not r['answers'] else []):
            k = (rec['type'], rec['data'])
            if k not in seen:
                seen.add(k)
                out['records'].append(rec)
    return out


# ---------- whois
def _whois_raw(server, q, timeout=8):
    with socket.create_connection((server, 43), timeout) as c:
        c.settimeout(timeout)
        c.sendall((q + '\r\n').encode('idna' if not q.isascii() else 'ascii', 'ignore'))
        buf = b''
        while len(buf) < 200000:
            x = c.recv(8192)
            if not x:
                break
            buf += x
    return buf.decode('utf-8', 'replace')


FIELDS = [('Registrar', r'^\s*(registrar|sponsoring registrar|registrar name)\s*:\s*(.+)$'),
          ('Registrovan', r'^\s*(creation date|created|registered|registration time|domain record activated|regdate)\s*:\s*(.+)$'),
          ('Ističe', r'^\s*(registry expiry date|registrar registration expiration date|expir\w* date|expires|paid-till|expiration time)\s*:\s*(.+)$'),
          ('Izmenjen', r'^\s*(updated date|last[- ]modified|changed|modified|last updated)\s*:\s*(.+)$'),
          ('Status', r'^\s*(domain status|status|state)\s*:\s*(.+)$'),
          ('Name serveri', r'^\s*(name server|nserver|nameservers?|dns)\s*:\s*(.+)$'),
          ('Vlasnik', r'^\s*(registrant organization|registrant|org-name|orgname|organization|owner|descr)\s*:\s*(.+)$'),
          ('Država', r'^\s*(registrant country|country)\s*:\s*(.+)$'),
          ('Mreža', r'^\s*(inetnum|inet6num|netrange|cidr|route)\s*:\s*(.+)$'),
          ('Ime mreže', r'^\s*(netname|net-name)\s*:\s*(.+)$'),
          ('AS', r'^\s*(origin|originas|aut-num)\s*:\s*(.+)$'),
          ('Abuse', r'^\s*(abuse-mailbox|orgabuseemail|registrar abuse contact email)\s*:\s*(.+)$')]


def whois(q):
    """IANA kaže koji registar je nadležan; za .com/.net registar kaže i registrara (detaljniji podaci)."""
    q = q.strip().rstrip('.')
    chain, texts = [], []
    try:
        ipaddress.ip_address(q)
        is_ip = True
    except ValueError:
        is_ip = False
        q = q.encode('idna').decode()
    srv = 'whois.iana.org'
    for _ in range(3):
        t = _whois_raw(srv, ('n + ' + q) if srv == 'whois.arin.net' else q)
        chain.append(srv)
        texts.append(t)
        m = re.search(r'^\s*(refer|whois|registrar whois server|ReferralServer)\s*:\s*(?:whois://)?([\w.-]+)', t, re.I | re.M)
        nxt = m.group(2).strip().lower() if m else None
        if not nxt or nxt in chain or (srv != 'whois.iana.org' and is_ip):
            break
        srv = nxt
    fields = []
    body = texts[-1] if len(texts) > 1 else texts[0]
    for label, rx in FIELDS:
        vals = []
        for t in reversed(texts[1:] or texts):
            for m in re.finditer(rx, t, re.I | re.M):
                v = m.group(2).strip()
                if v and v.lower() not in (x.lower() for x in vals) and not v.lower().startswith('http'):
                    vals.append(v[:120])
            if vals:
                break
        if vals:
            fields.append([label, vals[:8] if label in ('Name serveri', 'Status') else vals[:2]])
    return {'q': q, 'servers': chain, 'fields': fields, 'raw': '\n\n'.join('### %s\n%s' % (s, t.strip()) for s, t in zip(chain, texts))[:30000],
            'empty': not body.strip() or bool(re.search(r'no match|not found|no entries found|no data found', body, re.I))}
