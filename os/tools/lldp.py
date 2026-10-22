"""Teco.Pi: na koji port sviča / rutera je zakačen kabl — sluša LLDP i CDP okvire na interfejsu (treba root).
Upravljivi svičevi i ruteri ih šalju na 30 s (CDP 60 s); obični kućni ruteri uglavnom ne.

sudo python3 lldp.py eth0 35   ->  JSON na stdout: {"proto": "LLDP", "sys": ..., "port": ..., ...} ili {}
"""
import json
import select
import socket
import struct
import sys
import time

ETH_P_ALL = 0x0003
LLDP_MAC, CDP_MAC = b'\x01\x80\xc2\x00\x00\x0e', b'\x01\x00\x0c\xcc\xcc\xcc'


def txt(b):
    return b.decode('utf-8', 'replace').strip('\x00 ').strip()


def mac(b):
    return ':'.join('%02x' % x for x in b)


def lldp(p):
    """TLV: 7 bita tip, 9 bita dužina."""
    r, i = {'proto': 'LLDP'}, 0
    while i + 2 <= len(p):
        h = struct.unpack('!H', p[i:i + 2])[0]
        t, n = h >> 9, h & 0x1FF
        v = p[i + 2:i + 2 + n]
        i += 2 + n
        if t == 0:
            break
        if t == 1 and v:   # chassis: 4 = MAC, ostalo tekst
            r['chassis'] = mac(v[1:]) if v[0] == 4 and len(v) == 7 else txt(v[1:])
        elif t == 2 and v:   # port: 3 = MAC, ostalo (ime / broj) tekst
            r['port'] = mac(v[1:]) if v[0] == 3 and len(v) == 7 else txt(v[1:])
        elif t == 4:
            r['pdesc'] = txt(v)
        elif t == 5:
            r['sys'] = txt(v)
        elif t == 6:
            r['sdesc'] = txt(v)[:120]
        elif t == 8 and len(v) > 2:   # upravljačka adresa
            ln, fam = v[0], v[1]
            if fam == 1 and ln == 5:
                r['mgmt'] = socket.inet_ntoa(v[2:6])
        elif t == 127 and len(v) >= 6 and v[:3] == b'\x00\x80\xc2' and v[3] == 1:   # 802.1: VLAN (PVID)
            r['vlan'] = struct.unpack('!H', v[4:6])[0]
        elif t == 127 and len(v) >= 9 and v[:3] == b'\x00\x12\x0f' and v[3] == 1:   # 802.3: brzina/duplex
            r['autoneg'] = bool(v[4] & 2)
    return r


def cdp(p):
    """CDP: posle LLC/SNAP zaglavlja (8 B) i CDP zaglavlja (4 B) TLV: tip 2 B, dužina 2 B (sa zaglavljem)."""
    r, i = {'proto': 'CDP'}, 12
    while i + 4 <= len(p):
        t, n = struct.unpack('!HH', p[i:i + 4])
        if n < 4:
            break
        v = p[i + 4:i + n]
        i += n
        if t == 1:
            r['sys'] = txt(v)
        elif t == 3:
            r['port'] = txt(v)
        elif t == 5:
            r['sdesc'] = txt(v)[:120]
        elif t == 6:
            r['platform'] = txt(v)
        elif t == 10 and len(v) >= 2:
            r['vlan'] = struct.unpack('!H', v[:2])[0]
    return r


def main():
    ifc = sys.argv[1] if len(sys.argv) > 1 else 'eth0'
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 35
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    s.bind((ifc, 0))
    # multicast adrese LLDP/CDP (neki drajveri ih inače ne propuštaju)
    for m in (LLDP_MAC, CDP_MAC):
        try:
            mreq = struct.pack('iHH8s', socket.if_nametoindex(ifc), 0, 6, m + b'\x00\x00')   # PACKET_MR_MULTICAST
            s.setsockopt(263, 1, mreq)   # SOL_PACKET, PACKET_ADD_MEMBERSHIP
        except OSError:
            pass
    end = time.time() + wait
    while time.time() < end:
        if not select.select([s], [], [], max(0.1, end - time.time()))[0]:
            continue
        f = s.recv(4096)
        if len(f) < 16:
            continue
        dst, src, typ = f[:6], f[6:12], struct.unpack('!H', f[12:14])[0]
        try:
            if typ == 0x88CC:
                r = lldp(f[14:])
            elif dst == CDP_MAC and f[14:17] == b'\xaa\xaa\x03':
                r = cdp(f[14:])
            else:
                continue
        except (struct.error, IndexError):
            continue
        r['src'] = mac(src)
        print(json.dumps(r, ensure_ascii=False))
        return
    print('{}')


if __name__ == '__main__':
    main()
