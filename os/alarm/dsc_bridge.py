"""Teco.Pi <-> DSC alarm (PowerSeries) preko EnvisaLink (EVL-3 / EVL-4) — TPI protokol, TCP 4025.

Isti lokalni API kao Paradox most (alarm_bridge.py), da server i stranica rade isto za oba modela:
  GET  /state, /wait?v=N           -> veza, particije, zone, sistem (smetnje), dnevnik
  POST /partition {"p": id|"all", "cmd": "arm"|"arm_stay"|"arm_instant"|"arm_force"|"disarm"}
  POST /panic {"type": "emergency"|"medical"|"fire"}, /time, /events/clear, /events/range
Pokretanje: python3 dsc_bridge.py <id>   (podešavanje: data/alarm-<id>.json, chmod 600)
  host, port (4025), tpi_password (lozinka EnvisaLink-a, fabrički „user“), code (korisnički kod za uključ./isključ.),
  partitions (broj, 1), zones (broj, 8), names {"1": "Ulaz", …}, api_port, monitor [zone]
TPI: komanda = 3 cifre + podaci + kontrolni zbir (2 hex) + CRLF; centrala šalje isto (609 zona otvorena, 652 uključeno…).
"""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from aiohttp import web

AID = sys.argv[1] if len(sys.argv) > 1 else 'a2'
DATA = Path(__file__).resolve().parent.parent / 'data'
CFG_FILE = DATA / ('alarm-%s.json' % AID)
EV_FILE = DATA / ('alarm-events-%s.json' % AID)
EV_MAX = 1000

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s dsc-' + AID + ': %(message)s')
log = logging.getLogger('teco-dsc')

state = {'run': 'stop', 'err': None, 'since': None, 'panel': 'DSC (EnvisaLink)'}
parts, zones, troubles = {}, {}, {}
events, version, changed_ev = [], 0, None
writer, need_code = None, None   # TPI veza; particija koja traži kod (900)


def load_cfg():
    try:
        return json.loads(CFG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def bump():
    global version
    version += 1
    if changed_ev is not None:
        changed_ev.set()


def load_events():
    global events
    try:
        events = json.loads(EV_FILE.read_text())[-EV_MAX:]
    except (OSError, ValueError):
        events = []


def save_events():
    try:
        EV_FILE.write_text(json.dumps(events[-EV_MAX:], ensure_ascii=False))
    except OSError as e:
        log.warning('dnevnik: %s', e)


def push(typ, label, msg, change=None, tags=(), lvl='INFO'):
    events.append({'t': int(time.time()), 'type': typ, 'label': label, 'msg': msg, 'lvl': lvl, 'tags': list(tags), 'change': change or {}})
    del events[:-EV_MAX]
    save_events()
    bump()


def zone_label(n):
    return (load_cfg().get('names') or {}).get(str(n)) or str(n)


def init_layout():
    c = load_cfg()
    for p in range(1, max(1, int(c.get('partitions') or 1)) + 1):
        parts.setdefault(p, {'id': p, 'key': str(p), 'label': 'Particija %d' % p, 'arm': False, 'arm_stay': False, 'arm_no_entry': False,
                             'alarm': False, 'ready': True, 'exit_delay': False, 'entry_delay': False})
    for z in range(1, max(1, min(64, int(c.get('zones') or 8))) + 1):
        zones.setdefault(z, {'id': z, 'key': str(z), 'label': zone_label(z), 'open': False, 'alarm': False, 'tamper': False, 'bypassed': False})


def checksum(s):
    return '%02X' % (sum(s.encode()) & 0xFF)


async def send(cmd, data=''):
    if writer is None:
        raise ConnectionError('nema veze')
    pkt = cmd + data
    writer.write((pkt + checksum(pkt) + '\r\n').encode())
    await writer.drain()


def code6(c):   # TPI traži 6 cifara: 4-cifreni kod dopunjen sa 00
    c = ''.join(ch for ch in str(c or '') if ch.isdigit())
    return (c + '00')[:6] if len(c) == 4 else c[:6]


def zone_set(n, **kw):
    z = zones.setdefault(n, {'id': n, 'key': str(n), 'label': zone_label(n), 'open': False, 'alarm': False, 'tamper': False, 'bypassed': False})
    ch = {k: v for k, v in kw.items() if z.get(k) != v}
    z.update(kw)
    if not ch:
        return
    mon = {int(x) for x in load_cfg().get('monitor', [])}
    if 'open' in ch and n not in mon:   # otvaranje/zatvaranje samo za praćene zone; alarm i sabotaža uvek
        bump()
        return
    if 'open' in ch and not ch['open']:
        bump()
        return
    push('zone', z['label'], '', change=ch, tags=['alarm'] if ch.get('alarm') or ch.get('tamper') else [])


def part_set(p, ev=None, **kw):
    q = parts.setdefault(p, {'id': p, 'key': str(p), 'label': 'Particija %d' % p})
    ch = {k: v for k, v in kw.items() if q.get(k) != v}
    q.update(kw)
    if ev and ch:
        push('partition', q['label'], '', change={ev: kw.get(ev, True)}, tags=['alarm'] if ev == 'alarm' and kw.get('alarm') else
             ['arm'] if ev == 'arm' and kw.get('arm') else ['disarm'] if ev == 'arm' else [])
    else:
        bump()


TROUBLE = {'800': ('battery_trouble', True), '801': ('battery_trouble', False), '802': ('ac_trouble', True), '803': ('ac_trouble', False),
           '806': ('bell_trouble', True), '807': ('bell_trouble', False), '814': ('ftc_trouble', True), '829': ('tamper_trouble', True), '830': ('tamper_trouble', False)}


def handle(cmd, data):
    global need_code
    if cmd in ('609', '610'):
        zone_set(int(data[:3]), open=cmd == '609')
    elif cmd in ('601', '602'):
        zone_set(int(data[1:4]), alarm=cmd == '601')
    elif cmd in ('603', '604'):
        zone_set(int(data[1:4]), tamper=cmd == '603')
    elif cmd in ('650', '651', '653'):
        part_set(int(data[:1]), ready=cmd != '651')
    elif cmd == '652':   # uključeno: 0 away, 1 stay, 2 away bez ulaznog, 3 stay bez ulaznog
        mode = data[1:2] or '0'
        part_set(int(data[:1]), 'arm', arm=True, arm_stay=mode in '13', arm_no_entry=mode in '23', exit_delay=False, entry_delay=False)
    elif cmd == '654':
        part_set(int(data[:1]), 'alarm', alarm=True)
    elif cmd == '655':
        p = int(data[:1])
        part_set(p, 'arm', arm=False, arm_stay=False, arm_no_entry=False, alarm=False, exit_delay=False, entry_delay=False)
        for z in zones.values():
            z['alarm'] = False
    elif cmd == '656':
        part_set(int(data[:1]), exit_delay=True)
    elif cmd == '657':
        part_set(int(data[:1]), entry_delay=True)
    elif cmd == '659':
        push('partition', parts.get(int(data[:1]), {}).get('label', 'Particija'), 'Uključivanje nije uspelo (otvorena zona?)', tags=['trouble'])
    elif cmd in ('840', '841'):
        troubles['trouble_led'] = cmd == '840'
        bump()
    elif cmd in TROUBLE:
        k, on = TROUBLE[cmd]
        if troubles.get(k) != on:
            troubles[k] = on
            push('system', k, '', change={k: on}, tags=['trouble'] if on else [])
    elif cmd == '900':   # centrala traži kod
        need_code = data[:1]
        c = load_cfg().get('code')
        if c:
            asyncio.get_event_loop().create_task(send('200', code6(c)))
    elif cmd == '502':
        log.warning('sistemska greška TPI %s', data)
    elif cmd == '501':
        log.warning('neispravna komanda (kontrolni zbir)')


async def run():
    global writer
    while True:
        c = load_cfg()
        if not c.get('enabled') or not c.get('host'):
            state.update(run='off', err='Alarm nije podešen (Alarm → Podešavanje).')
            bump()
            await asyncio.sleep(5)
            continue
        init_layout()
        state.update(run='connecting', err=None, since=time.time())
        bump()
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(c['host'], int(c.get('port') or 4025)), 10)
            logged = False
            last_poll = time.time()
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), 30)
                except asyncio.TimeoutError:
                    line = None
                if time.time() - last_poll > 60 and logged:   # keep-alive
                    await send('000')
                    last_poll = time.time()
                if line is None:
                    continue
                if not line:
                    raise ConnectionError('veza prekinuta')
                s = line.decode(errors='replace').strip()
                if len(s) < 5:
                    continue
                cmd, data = s[:3], s[3:-2]
                if cmd == '505':   # prijava: 3 = pošalji lozinku, 1 = uspela, 0 = pogrešna lozinka
                    if data.startswith('3'):
                        await send('005', str(c.get('tpi_password') or 'user'))
                    elif data.startswith('1'):
                        logged = True
                        state.update(run='run', err=None, since=time.time())
                        log.info('prijavljen na EnvisaLink %s', c['host'])
                        await send('001')   # pun izveštaj stanja (zone, particije)
                        bump()
                    else:
                        raise PermissionError('pogrešna lozinka EnvisaLink-a')
                    continue
                handle(cmd, data)
        except PermissionError as e:
            state.update(run='error', err=str(e))
            await asyncio.sleep(30)
        except (OSError, ConnectionError, asyncio.TimeoutError) as e:
            state.update(run='error', err='Nema veze sa EnvisaLink-om (%s)' % (e.__class__.__name__ if not str(e) else e))
            await asyncio.sleep(10)
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            writer = None
            bump()


def snapshot():
    s = dict(state)
    s['partitions'] = [dict(p) for _, p in sorted(parts.items())]
    s['zones'] = [dict(z) for _, z in sorted(zones.items())]
    s['pgms'] = []
    s['system'] = {'troubles': dict(troubles)}
    s['site'] = {'model': 'DSC PowerSeries', 'ip_type': 'EnvisaLink', 'host': load_cfg().get('host')}
    s['events'] = events[-200:]
    s['v'] = version
    return s


async def h_state(request):
    return web.json_response(snapshot())


async def h_wait(request):
    global changed_ev
    try:
        v = int(request.query.get('v', -1))
    except ValueError:
        v = -1
    if v == version:
        changed_ev = changed_ev or asyncio.Event()
        changed_ev.clear()
        try:
            await asyncio.wait_for(changed_ev.wait(), 20)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.05)
    return web.json_response(snapshot())


def ready():
    return writer is not None and state['run'] == 'run'


async def h_partition(request):
    a = await request.json()
    cmd = str(a.get('cmd') or '')
    if not ready():
        return web.json_response({'err': 'Alarm nije povezan.'}, status=503)
    code = code6(load_cfg().get('code'))
    ps = list(parts) if str(a.get('p') or 'all') == 'all' else [int(a['p'])]
    try:
        for p in ps:
            if cmd == 'disarm':
                if not code:
                    return web.json_response({'err': 'Za isključivanje upiši korisnički kod u podešavanju alarma.'})
                await send('040', '%d%s' % (p, code))
            elif cmd in ('arm', 'arm_force'):
                await send('030', str(p))
            elif cmd == 'arm_stay':
                await send('031', str(p))
            elif cmd == 'arm_instant':
                await send('032', str(p))
            else:
                return web.json_response({'err': 'Nepoznata komanda.'}, status=400)
    except (OSError, ConnectionError) as e:
        return web.json_response({'err': str(e)})
    log.info('particije %s: %s', ps, cmd)
    return web.json_response({'ok': True})


async def h_panic(request):
    a = await request.json()
    t = {'fire': '1', 'medical': '2', 'emergency': '3'}.get(str(a.get('type') or 'emergency'))
    if not ready() or not t:
        return web.json_response({'err': 'Alarm nije povezan.'}, status=503)
    await send('060', t)
    log.warning('PANIC %s', a.get('type'))
    return web.json_response({'ok': True})


async def h_time(request):
    if not ready():
        return web.json_response({'err': 'Alarm nije povezan.'}, status=503)
    await send('010', time.strftime('%H%M%m%d%y'))
    return web.json_response({'ok': True, 'time': time.strftime('%d.%m.%Y %H:%M'), 'drift': None})


async def h_unsupported(request):
    return web.json_response({'err': 'DSC preko EnvisaLink-a ne podržava ovu komandu.'})


async def h_events_range(request):
    a = await request.json()
    t0, t1 = int(a.get('from') or 0), int(a.get('to') or 0) or int(time.time()) + 60
    return web.json_response({'events': [e for e in events if t0 <= e['t'] <= t1][-500:]})


async def h_events_clear(request):
    events.clear()
    save_events()
    bump()
    return web.json_response({'ok': True})


async def main():
    load_events()
    app = web.Application()
    app.router.add_get('/state', h_state)
    app.router.add_get('/wait', h_wait)
    app.router.add_post('/partition', h_partition)
    app.router.add_post('/panic', h_panic)
    app.router.add_post('/time', h_time)
    app.router.add_post('/pgm', h_unsupported)
    app.router.add_post('/zone', h_unsupported)
    app.router.add_post('/events/clear', h_events_clear)
    app.router.add_post('/events/range', h_events_range)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = int(load_cfg().get('api_port') or 8111)
    await web.TCPSite(runner, '127.0.0.1', port).start()
    log.info('Teco.Pi DSC most na 127.0.0.1:%d', port)
    await run()


if __name__ == '__main__':
    asyncio.run(main())
