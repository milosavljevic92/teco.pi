"""Web monitor (Ekstenzije): da li javna web aplikacija radi — kao updown.io.

Provera: GET (prati preusmerenja), status < 400, opciono ključna reč na stranici (ili reč koje NE sme da bude),
vreme odziva. SSL: datum isteka sertifikata (i da li je ispravan) — proverava se ređe (na 6 h).
"""
import asyncio
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

UA = 'Teco.Pi web monitor (+https://tecomatic.rs)'


async def check(session, url, kw='', kw_not=False, timeout=10):
    """-> {'ok': bool, 'code': int|None, 'ms': int|None, 'err': str|None}"""
    import aiohttp
    t0 = time.monotonic()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True,
                               headers={'User-Agent': UA, 'Cache-Control': 'no-cache'}) as r:
            body = await r.content.read(2 * 2**20) if kw else b''
            ms = int((time.monotonic() - t0) * 1000)
            if r.status >= 400:
                return {'ok': False, 'code': r.status, 'ms': ms, 'err': 'HTTP %d' % r.status}
            if kw:
                has = kw.lower() in body.decode(r.charset or 'utf-8', errors='replace').lower()
                if has == kw_not:
                    return {'ok': False, 'code': r.status, 'ms': ms,
                            'err': ('Pronađena reč „%s“' if kw_not else 'Nema reči „%s“') % kw}
            return {'ok': True, 'code': r.status, 'ms': ms, 'err': None}
    except asyncio.TimeoutError:
        return {'ok': False, 'code': None, 'ms': None, 'err': 'Ne odgovara (%d s)' % timeout}
    except aiohttp.ClientConnectorCertificateError:
        return {'ok': False, 'code': None, 'ms': None, 'err': 'Neispravan SSL sertifikat'}
    except aiohttp.ClientConnectorError as e:
        return {'ok': False, 'code': None, 'ms': None, 'err': 'Nema veze (%s)' % (getattr(e, 'os_error', None) or e).__class__.__name__}
    except aiohttp.ClientError as e:
        return {'ok': False, 'code': None, 'ms': None, 'err': str(e)[:80] or e.__class__.__name__}


def cert_info(url, timeout=8):
    """Sertifikat servera: {'until': epoch, 'days': int, 'issuer': str} ili {'err': ...} (blokirajuće — u executor-u)."""
    u = urlsplit(url)
    host, port = u.hostname, u.port or 443
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s, ctx.wrap_socket(s, server_hostname=host) as ss:
            c = ss.getpeercert()
    except ssl.SSLCertVerificationError as e:
        return {'err': 'Sertifikat nije ispravan: %s' % (e.verify_message or e.reason)}
    except (OSError, ssl.SSLError) as e:
        return {'err': 'SSL: %s' % (e.strerror or e.__class__.__name__) if isinstance(e, OSError) else 'SSL: %s' % e.reason}
    until = datetime.strptime(c['notAfter'], '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc).timestamp()
    issuer = dict(x[0] for x in c.get('issuer', ())).get('organizationName', '')
    return {'until': until, 'days': int((until - time.time()) // 86400), 'issuer': issuer}
