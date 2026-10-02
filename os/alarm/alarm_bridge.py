"""Teco.Pi <-> Paradox alarm (EVO + IP150) preko PAI (Paradox Alarm Interface).

Radi kao posebna usluga (teco-alarm.service, venv ~/.local/pai-venv), da problem sa alarmom ne obori Teco.Pi.
Drži stalnu vezu sa IP modulom i daje lokalni API samo za Teco.Pi server (127.0.0.1:8098):
  GET  /state                         -> veza, particije, zone, PGM, sistem (smetnje, napajanje), dnevnik
  GET  /wait?v=N                      -> isto, ali tek kad se stanje promeni (dugo čekanje, do 20 s)
  POST /partition {"p": key|id|"all", "cmd": "arm"|"arm_stay"|"arm_instant"|"arm_force"|"disarm"}
  POST /pgm       {"p": key|id, "cmd": "on"|"off"|"release"|"on_override"|"off_override"}
Pristupni podaci su u ~/teco/data/alarm.json (chmod 600), upisuje ih Admin forma.
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from aiohttp import web

DATA = Path(__file__).resolve().parent.parent / 'data'
CFG_FILE = DATA / 'alarm.json'
PAI_CFG = DATA / 'pai-config.json'   # generisano iz alarm.json (PAI čita konfiguraciju iz fajla)
PORT = 8098
CMDS = ('arm', 'arm_stay', 'arm_instant', 'arm_force', 'disarm')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('teco-alarm')

state = {'run': 'stop', 'err': None, 'since': None, 'panel': None}
alarm = None
EV_FILE = DATA / 'alarm-events.json'   # dnevnik događaja (poslednjih 300), ostaje posle restarta
events = []
version = 0                           # raste na svaku promenu: server čeka /wait?v= i dobija stanje odmah
changed_ev = None                     # asyncio.Event koji budi čekanje (/wait)


def bump():
    global version
    version += 1
    if changed_ev is not None:
        changed_ev.set()


def load_events():
    global events
    try:
        events = json.loads(EV_FILE.read_text())[-300:]
    except (OSError, ValueError):
        events = []


def save_events():
    try:
        EV_FILE.write_text(json.dumps(events[-300:], ensure_ascii=False))
    except OSError as e:
        log.warning('dnevnik: %s', e)


_save_h = None
REPORT_RE = re.compile(r'kiss.?off|telephone|communicat|dialer|report|tlm|ground start|listen-in', re.I)
REPORT_KEYS = {'tlm_trouble', 'dialer_trouble', 'com_pc_trouble', 'module_tlm_trouble', 'module_fail_to_com_trouble'}


def on_event(event=None, **kw):
    """Događaj sa centrale (PAI): zona, particija, korisnik, sistem. Čuva se u dnevniku."""
    global _save_h
    try:
        p = event.props if hasattr(event, 'props') else {}
        e = {'t': int(getattr(event, 'timestamp', 0) or time.time()),
             'type': str(getattr(event, 'type', '') or ''),
             'label': str(getattr(event, 'label', '') or ''),
             'msg': str(getattr(event, 'message', '') or ''),
             'lvl': getattr(getattr(event, 'level', None), 'name', ''),
             'tags': [str(t) for t in (getattr(event, 'tags', None) or [])][:6],
             'change': {k: v for k, v in (getattr(event, 'change', None) or {}).items() if isinstance(v, (bool, int, float, str))}}
        if not e['msg'] and not e['change']:
            return
        if e['lvl'] == 'DEBUG' or (e['type'] == 'system' and e['label'] == 'date'):
            return   # „Panel time is…“ na svake 4 s i sl. ne ide u dnevnik
        # dojava (PSTN/glasovna/IP): potvrda prijema, zvonjenje, neuspela dojava, telefonska linija…
        if REPORT_RE.search(e['msg']) or any(k in REPORT_KEYS or k.startswith('fail_central') for k in e['change']):
            e['cat'] = 'report'
        if e['type'] == 'partition' and ('zones closed' in e['msg'] or set(e['change']) & {'all_zone_closed', 'ready', 'ready_status'}):
            return   # „sve zone zatvorene / nije spremna“ prati svako otvaranje zone: šum
        if e['type'] == 'zone':   # zone: samo one sa „Monitor“ (bira se na stranici), osim alarma i sabotaže
            zid = zone_id(event)
            e['zid'] = zid
            if zid not in monitored() and not set(e['change']) & {'alarm', 'tamper', 'fire'} and 'alarm' not in e['tags']:
                return
        if events and all(events[-1].get(k) == e[k] for k in ('type', 'label', 'msg', 'change')) and e['t'] - events[-1]['t'] < 3:
            return   # isti događaj dvaput (živi događaj + promena stanja)
        events.append(e)
        del events[:-300]
        bump()
        loop = asyncio.get_event_loop()
        if _save_h:
            _save_h.cancel()
        _save_h = loop.call_later(3, save_events)   # upis na karticu najviše na 3 s
    except Exception as ex:
        log.warning('događaj: %s', ex)


def on_change(change=None, **kw):
    bump()


_mon = (None, set())


def monitored():
    """Zone sa „Monitor“ (alarm.json → monitor: [id]): samo njihova otvaranja/zatvaranja idu u dnevnik."""
    global _mon
    try:
        mt = CFG_FILE.stat().st_mtime
        if _mon[0] != mt:
            _mon = (mt, {int(x) for x in load_cfg().get('monitor', [])})
    except (OSError, ValueError, TypeError):
        pass
    return _mon[1]


def zone_id(event):
    """ID zone iz događaja: živi događaj ima id, promena stanja ima ključ/naziv zone."""
    zid = getattr(event, 'id', None)
    if isinstance(zid, int):
        return zid
    lab = getattr(event, 'label', None)
    try:
        for key, z in alarm.storage.get_container('zone').items():
            if key == lab or z.get('label') == lab or str(z.get('id')) == str(lab):
                return int(z.get('id') or key)
    except Exception:
        pass
    return None


def load_cfg():
    try:
        return json.loads(CFG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def write_pai_cfg(c):
    """Samo ono što treba za IP vezu; ostalo su PAI podrazumevane vrednosti (bez MQTT i ostalih interfejsa)."""
    pc = str(c.get('pc_password') or '').strip()
    conf = {
        'CONNECTION_TYPE': 'IP',
        'IP_CONNECTION_HOST': str(c.get('host') or ''),
        'IP_CONNECTION_PORT': int(c.get('port') or 10000),
        'IP_CONNECTION_PASSWORD': str(c.get('ip_password') or 'paradox'),
        'PASSWORD': pc or None,
        'KEEP_ALIVE_INTERVAL': 4,   # stanje (smetnje, napajanje) na 4 s; zone i particije stižu odmah kao događaji
        'SYNC_TIME': False,
        'MQTT_ENABLE': False,
        'IP_INTERFACE_ENABLE': False,
        'LOGGING_LEVEL_CONSOLE': logging.INFO,
        'LOGGING_FILE': None,
    }
    PAI_CFG.write_text(json.dumps(conf))
    os.chmod(PAI_CFG, 0o600)


def _plain(el, keys=None):
    """Element iz PAI memorije -> običan dict (samo jednostavne vrednosti)."""
    out = {}
    for k, v in dict(el).items():
        if keys and k not in keys:
            continue
        if isinstance(v, (bool, int, float, str)) or v is None:
            out[k] = v
    return out


def snapshot():
    s = dict(state)
    s['partitions'], s['zones'], s['pgms'] = [], [], []
    if alarm is not None and getattr(alarm, 'storage', None) is not None:
        try:
            for name, out in (('partition', 'partitions'), ('zone', 'zones'), ('pgm', 'pgms')):
                for key, el in alarm.storage.get_container(name).items():
                    d = _plain(el)
                    d['key'] = key
                    s[out].append(d)
        except Exception as e:   # memorija se još učitava
            s['err'] = s['err'] or str(e)
    for out in ('partitions', 'zones', 'pgms'):
        s[out].sort(key=lambda x: x.get('id') or 0)
    # sistem: smetnje (AC, akumulator, sirena, sabotaža, dojava…), napajanje (V), vreme centrale
    s['system'] = {}
    if alarm is not None and getattr(alarm, 'storage', None) is not None:
        try:
            for key, el in alarm.storage.get_container('system').items():
                if str(key) != 'date':   # sat centrale se menja stalno, a ne prikazujemo ga
                    s['system'][str(key)] = _plain(el)
        except Exception:
            pass
    s['events'] = events[-80:]
    s['v'] = version
    return s


def setstate(**kw):
    state.update(kw)
    bump()


async def run_alarm():
    """Povezivanje sa ponovnim pokušajima (kao PAI main.run_loop)."""
    global alarm
    from paradox.config import config as cfg
    from paradox.lib.encodings import register_encodings
    from paradox.paradox import Paradox

    from paradox.lib import ps

    c = load_cfg()
    if not c.get('enabled') or not c.get('host'):
        setstate(run='off', err='Alarm nije podešen (Admin → Alarm · veza).')
        return
    write_pai_cfg(c)
    cfg.load(str(PAI_CFG))
    register_encodings()
    ps.subscribe(on_event, 'events')     # živi događaji: dnevnik
    ps.subscribe(on_change, 'changes')   # svaka promena stanja: server dobija novo stanje odmah
    wait = 2
    while True:
        alarm = Paradox()
        setstate(run='connecting', err=None, since=time.time())
        try:
            if await alarm.full_connect():
                wait = 2
                panel = getattr(alarm, 'panel', None)
                setstate(run='run', err=None, since=time.time(),
                         panel=type(panel).__name__.replace('Panel_', '') if panel else None)
                log.info('povezan sa centralom (%s)', state['panel'])
                await alarm.loop()
                setstate(run='connecting', err='Veza sa centralom je prekinuta.')
            else:
                setstate(run='error', err='Povezivanje nije uspelo (proveri IP, port i lozinke; IP150 prima samo jednu vezu).')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception('greška')
            setstate(run='error', err=str(e)[:200])
        try:
            await alarm.disconnect()
        except Exception:
            pass
        await asyncio.sleep(wait)
        wait = min(wait * 2, 60)


async def h_state(request):
    return web.json_response(snapshot())


async def h_wait(request):
    """Dugo čekanje: odgovara čim se stanje promeni (v je poslednja verzija koju server ima), najviše 20 s."""
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
        await asyncio.sleep(0.05)   # skupi promene koje stižu zajedno
    return web.json_response(snapshot())


async def h_partition(request):
    a = await request.json()
    cmd = str(a.get('cmd') or '')
    if cmd not in CMDS:
        return web.json_response({'err': 'Nepoznata komanda.'}, status=400)
    if alarm is None or state['run'] != 'run':
        return web.json_response({'err': 'Alarm nije povezan.'}, status=503)
    p = str(a.get('p') or 'all')
    ok = await alarm.control_partition(p, cmd)
    log.info('particija %s: %s -> %s', p, cmd, ok)
    return web.json_response({'ok': bool(ok)} if ok else {'err': 'Centrala nije prihvatila komandu.'})


PGM_CMDS = ('on', 'off', 'release', 'on_override', 'off_override')


async def h_pgm(request):
    a = await request.json()
    cmd = str(a.get('cmd') or '')
    if cmd not in PGM_CMDS:
        return web.json_response({'err': 'Nepoznata komanda.'}, status=400)
    if alarm is None or state['run'] != 'run':
        return web.json_response({'err': 'Alarm nije povezan.'}, status=503)
    p = str(a.get('p') or '')
    ok = await pgm_override(p, cmd)
    if not ok:   # rezerva: obična PAI komanda (na ovoj EVO centrali ne dobija odgovor)
        ok = await alarm.control_output(p, cmd)
    log.info('PGM %s: %s -> %s', p, cmd, ok)
    return web.json_response({'ok': bool(ok)} if ok else {'err': 'Centrala nije prihvatila komandu.'})


async def h_zone(request):
    """Zone: brisanje memorije alarma (clear_alarm_memory), isključenje iz rada (bypass / clear_bypass)."""
    a = await request.json()
    cmd = str(a.get('cmd') or '')
    if cmd not in ('clear_alarm_memory', 'bypass', 'clear_bypass'):
        return web.json_response({'err': 'Nepoznata komanda.'}, status=400)
    if alarm is None or state['run'] != 'run':
        return web.json_response({'err': 'Alarm nije povezan.'}, status=503)
    p = str(a.get('p') or 'all')
    ok = await alarm.control_zone(p, cmd)
    log.info('zona %s: %s -> %s', p, cmd, ok)
    bump()
    return web.json_response({'ok': bool(ok)} if ok else {'err': 'Centrala nije prihvatila komandu.'})


async def h_events_clear(request):
    events.clear()
    save_events()
    bump()
    return web.json_response({'ok': True})


EVO_OVR = {'on': 'override_on', 'off': 'override_off', 'on_override': 'override_on', 'off_override': 'override_off', 'release': 'release'}


async def pgm_override(p, cmd):
    """EVO: PGM kao iz BabyWare statusnog prozora (broadcast „pgm_override“ ka centrali, adresa 0).
    PAI-jeva komanda 0x40 na EVO192 ostaje bez odgovora (timeout), a ovako centrala direktno menja izlaz.
    Centrala na ovaj paket ne šalje potvrdu koju PAI razume, pa se šalje jednom i smatra poslatim."""
    try:
        from paradox.hardware.evo import parsers as evp
        pid = int(p)
        if not 1 <= pid <= 16 or cmd not in EVO_OVR:
            return False
        data = evp.PGMBroadcastCommand.build({pid: EVO_OVR[cmd]})
        try:
            await alarm.send_wait(evp.BroadcastRequest, dict(sub_command='pgm_override', bus_address=0, data=data),
                                  retries=1, timeout=1)
        except asyncio.TimeoutError:
            pass   # nema potvrde — očekivano
        log.info('PGM %s: %s (pgm_override) poslato', pid, EVO_OVR[cmd])
        return True
    except Exception as e:
        log.warning('PGM override %s %s: %s', p, cmd, e)
        return False


async def main():
    app = web.Application()
    app.router.add_get('/state', h_state)
    app.router.add_get('/wait', h_wait)
    load_events()
    app.router.add_post('/partition', h_partition)
    app.router.add_post('/pgm', h_pgm)
    app.router.add_post('/zone', h_zone)
    app.router.add_post('/events/clear', h_events_clear)
    runner = web.AppRunner(app, access_log=None)   # server pita na 1,5 s: bez zapisa svakog upita (/tmp je u RAM-u)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', PORT).start()   # samo lokalno: spolja se ide preko Teco.Pi servera
    log.info('Teco.Pi alarm most na 127.0.0.1:%d', PORT)
    await run_alarm()
    while True:   # nije podešen: API i dalje odgovara (stanje „off“)
        await asyncio.sleep(3600)


if __name__ == '__main__':
    asyncio.run(main())
