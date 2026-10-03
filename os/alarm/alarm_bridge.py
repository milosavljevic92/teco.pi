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
EV_FILE = DATA / 'alarm-events.json'   # dnevnik događaja (poslednjih EV_MAX), ostaje posle restarta
EV_MAX = 1000
events = []
version = 0                           # raste na svaku promenu: server čeka /wait?v= i dobija stanje odmah
changed_ev = None                     # asyncio.Event koji budi čekanje (/wait)


site = {}   # podaci o centrali i IP modulu (iz poruka PAI pri povezivanju): model, verzija, serijski brojevi


class SiteLog(logging.Handler):
    """PAI podatke o centrali samo upiše u log pri povezivanju; ovde se pokupe za prikaz na stranici."""
    def emit(self, record):
        try:
            m = record.getMessage()
            r = re.search(r'Panel Identified (\S+) version (.+)$', m)
            if r:
                site.update(model=r.group(1), fw=r.group(2))
            r = re.search(r'IP\((\w+)\) Module version (\w+), firmware: ([\d.]+), serial: (\w+)', m)
            if r:
                site.update(ip_type=r.group(1), ip_fw=r.group(3), ip_serial=r.group(4).upper())
        except Exception:
            pass


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


_save_h = None
REPORT_RE = re.compile(r'kiss.?off|telephone|communicat|dialer|report|tlm|ground start|listen-in', re.I)
REPORT_KEYS = {'tlm_trouble', 'dialer_trouble', 'com_pc_trouble', 'module_tlm_trouble', 'module_fail_to_com_trouble'}


RAW_LOG = None   # dijagnostika: '/tmp/teco-alarm-ev.log' upisuje svaki događaj pre filtera (tmpfs, raste brzo)
PART_KEEP = {'current_state'}   # od promena stanja particije samo ova; ostalo (memorija, sirena, ciljno stanje…) je šum


def _ts(v):
    """Vreme događaja: živi događaji imaju datetime (sat centrale), promene stanja broj."""
    if hasattr(v, 'timestamp'):
        return int(v.timestamp())
    try:
        return int(v) or int(time.time())
    except (TypeError, ValueError):
        return int(time.time())


def on_event(event=None, **kw):
    """Događaj sa centrale (PAI): zona, particija, korisnik, sistem. Čuva se u dnevniku."""
    global _save_h
    try:
        live = hasattr(event, 'major')   # živi događaj sa centrale (odmah), inače promena stanja (na 4 s)
        if RAW_LOG:   # privremeno: svaki događaj pre filtera (dijagnostika zona)
            with open(RAW_LOG, 'a') as f:
                f.write('%s live=%s type=%s id=%s label=%r lvl=%s change=%s tags=%s msg=%r\n' % (
                    time.strftime('%H:%M:%S'), live, getattr(event, 'type', None), getattr(event, 'id', None), getattr(event, 'label', None),
                    getattr(getattr(event, 'level', None), 'name', ''), getattr(event, 'change', None), getattr(event, 'tags', None),
                    getattr(event, 'message', '')))
        if live and getattr(event, 'type', '') in ('partition', 'user', 'system') and getattr(event, 'label', '') != 'date':
            full_status_now()   # uključenje/isključenje sa tastature i sl.: stanje particije odmah
        e = {'t': _ts(getattr(event, 'timestamp', 0)),
             'type': str(getattr(event, 'type', '') or ''),
             'label': str(getattr(event, 'label', '') or ''),
             'msg': str(getattr(event, 'message', '') or ''),
             'lvl': getattr(getattr(event, 'level', None), 'name', ''),
             'tags': [str(t) for t in (getattr(event, 'tags', None) or [])][:6],
             'change': {k: v for k, v in (getattr(event, 'change', None) or {}).items() if isinstance(v, (bool, int, float, str))}}
        if not e['msg'] and not e['change']:
            return
        if re.search(r'WinLoad (in|out)', e['msg'], re.I):
            return   # povezivanje softvera (Teco.Pi most, Swan, WinLoad) na centralu: stalno se ponavlja, ne znači ništa
        # zona otvorena / zatvorena (PAI: DEBUG). Ova EVO centrala preko IP150 ne šalje žive događaje zona:
        # stižu samo kao promena stanja (čitanje na KEEP_ALIVE_INTERVAL), pa se uzimaju odatle
        zone_oc = e['type'] == 'zone' and set(e['change']) == {'open'}
        if zone_oc and not e['change']['open']:
            return   # u dnevnik ide samo otvaranje zone (izbor korisnika), zatvaranje ne
        if (e['lvl'] == 'DEBUG' and not zone_oc) or (e['type'] == 'system' and e['label'] == 'date'):
            return   # „Panel time is…“ na svake 4 s i sl. ne ide u dnevnik
        # dojava (PSTN/glasovna/IP): potvrda prijema, zvonjenje, neuspela dojava, telefonska linija…
        if (REPORT_RE.search(e['msg']) and not re.search(r'non-?reportable', e['msg'], re.I)) \
                or any(k in REPORT_KEYS or k.startswith('fail_central') for k in e['change']):   # „Non-reportable event“ nije dojava
            e['cat'] = 'report'
        if e['type'] == 'partition' and ('zones closed' in e['msg'] or set(e['change']) & {'all_zone_closed', 'ready', 'ready_status'}):
            return   # „sve zone zatvorene / nije spremna“ prati svako otvaranje zone: šum
        if e['type'] == 'partition' and not live and e.get('cat') != 'report' and not set(e['change']) & PART_KEEP:
            return   # jedan krug uključi/alarm/isključi pravio je ~15 upisa i gurao zone iz dnevnika
        if e['type'] == 'zone':   # zone: otvaranje/zatvaranje samo za zone sa „Monitor“; alarm, sabotaža, požar uvek
            zid = zone_id(event)
            e['zid'] = zid
            important = set(e['change']) & {'alarm', 'tamper', 'fire', 'generated_alarm', 'presently_in_alarm', 'fire_alarm', 'zone_tamper_trouble'} \
                or set(e['tags']) & {'alarm', 'trouble'}
            if zid not in monitored() and not important:
                return
        same = ('type', 'zid', 'change') if e['type'] == 'zone' else ('type', 'label', 'msg', 'change')   # zona: živi i promena imaju različit naziv
        if any(all(o.get(k) == e.get(k) for k in same) and abs(e['t'] - o['t']) < 8 for o in events[-6:]):
            return   # isti događaj dvaput (živi događaj + promena stanja)
        push_event(e)
    except Exception as ex:
        log.warning('događaj: %s', ex)


def push_event(e):
    global _save_h
    events.append(e)
    del events[:-EV_MAX]
    bump()
    loop = asyncio.get_event_loop()
    if _save_h:
        _save_h.cancel()
    _save_h = loop.call_later(3, save_events)   # upis na karticu najviše na 3 s


# Dojava: centrala o pozivu ne šalje događaje, ali svaki njen odgovor nosi zastavicu „alarm reporting pending“
# (ima poruku za slanje / bira), a RAM blok 1 zastavice linije (zvono, kiss off = prijem potvrđen).
rep = {'pending': None, 'ring': None, 'kiss': None, 't0': None}


def report_event(msg, tags):
    push_event({'t': int(time.time()), 'type': 'system', 'label': '', 'msg': msg, 'lvl': 'INFO',
                'tags': tags, 'change': {}, 'cat': 'report'})
    log.info('dojava: %s', msg)


def report_watch(message, res):
    try:
        v = message.fields.value
        pend = bool(getattr(getattr(v.po, 'status', None), 'alarm_reporting_pending', False))
        if rep['pending'] is not None and pend != rep['pending']:
            if pend:
                rep['t0'] = time.time()
                report_event('Dojava počela: centrala bira broj', ['trouble'])
            else:
                dur = int(time.time() - (rep['t0'] or time.time()))
                report_event('Dojava završena' + (' (%d s)' % dur if dur else ''), ['disarm'])
        rep['pending'] = pend
        if res is not None and v.address == 1:
            sf = res.get('_system_flags') or {}
            ring, kiss = bool(sf.get('line_ring')), bool(sf.get('kiss_off'))
            if rep['ring'] is not None and ring and not rep['ring']:
                report_event('Telefonska linija: zvono', ['info'])
            if rep['kiss'] is not None and kiss and not rep['kiss']:
                report_event('Dojava potvrđena (kiss off)', ['disarm'])
            rep['ring'], rep['kiss'] = ring, kiss
    except Exception as ex:
        log.warning('dojava (status): %s', ex)


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
            if lab in (key, z.get('key'), z.get('label')) or str(z.get('id')) == str(lab):   # promena stanja: ključ („Hodnik_IC“)
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
        # pauza između čitanja stanja: ova centrala zone ne javlja sama (nema živih događaja zona), pa je ovo kašnjenje
        # senzora; samo čitanje preko IP150 traje ~1,4 s, pa je ceo krug ~2,4 s
        'KEEP_ALIVE_INTERVAL': 1,
        # odgovor centrale preko IP150 zna da kasni (Wi-Fi): sa 0,5 s (PAI podrazumevano) stizao je posle isteka
        # („Already handled / No handler for message 5“) i PAI je prekidao vezu na ~5 min
        'IO_TIMEOUT': 2.0,
        'LIMITS': {'door': [], 'module': []},   # vrata i moduli se ne koriste: brže povezivanje (~8 s manje)
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
    s['site'] = dict(site, host=load_cfg().get('host'))
    st = getattr(getattr(alarm, 'panel', None), 'settings', None)   # serijski broj centrale, ako ga PAI ima
    if st is not None:
        for k in ('serial_number', 'panel_id'):
            v = getattr(st, k, None) if not isinstance(st, dict) else st.get(k)
            if isinstance(v, (bytes, bytearray)):
                s['site']['serial'] = v.hex().upper()
                break
    try:   # korisnici sa imenom (ne „User 005“…)
        s['site']['users'] = [u.get('label') for _, u in alarm.storage.get_container('user').items()
                              if u.get('label') and not re.fullmatch(r'User \d+', u.get('label'))]
    except Exception:
        pass
    s['events'] = events[-200:]
    s['v'] = version
    return s


def setstate(**kw):
    state.update(kw)
    bump()


FULL_EVERY = 10   # ceo status (particije, PGM, alarm/bypass zona) na svaki 10. krug (~15 s) i odmah posle događaja particije/komande


def fast_status(panel):
    """PAI na svakom krugu čita sve RAM blokove centrale (~3,3 s preko IP150), a otvorenost zona, smetnje i napajanje
    su u bloku 1. Brzi krug čita samo blok 1 (~0,5 s), pa senzor kasni ~1 s umesto 6-7 s."""
    orig = panel.get_status_requests
    orig_hs = panel.handle_status

    def hs(message, parser_map):   # svaki odgovor na čitanje stanja: zastavice dojave (report_watch)
        res = orig_hs(message, parser_map)
        report_watch(message, res)
        return res
    panel.handle_status = hs
    addrs = list(getattr(panel, 'status_request_addresses', []) or [])
    if 1 not in addrs:
        return
    n = [0]

    def get():
        n[0] += 1
        if n[0] % FULL_EVERY == 1 or getattr(alarm, 'teco_full', False):
            alarm.teco_full = False
            return orig()
        return (panel.request_status(i) for i in (1,))
    panel.get_status_requests = get


def full_status_now():
    """Posle komande (uključi/isključi, PGM, zona): sledeći krug odmah i ceo."""
    if alarm is not None:
        alarm.teco_full = True
        try:
            alarm.request_status_refresh()
        except Exception:
            pass


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
    # JEDNA instanca za sve pokušaje (kao PAI main): Paradox() se pri pravljenju pretplaćuje na promene,
    # pa je nova instanca na svaki prekid množila događaje (2×, 4×…) i usporavala most
    alarm = Paradox()
    while True:
        setstate(run='connecting', err=None, since=time.time())
        try:
            if await alarm.full_connect():
                wait = 2
                panel = getattr(alarm, 'panel', None)
                if panel is not None:
                    fast_status(panel)
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
    full_status_now()
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
    via = 'broadcast'   # ova centrala odgovara na pgm_override; 0x40 (PAI) ostaje bez odgovora — samo rezerva
    ok = await pgm_override(p, cmd)
    if not ok:
        via = '0x40'
        try:
            ok = await alarm.control_output(p, cmd)
        except Exception as e:
            log.warning('PGM %s %s (0x40): %s', p, cmd, e)
            ok = False
    log.info('PGM %s: %s -> %s (%s)', p, cmd, ok, via)
    full_status_now()
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
    full_status_now()
    bump()
    return web.json_response({'ok': bool(ok)} if ok else {'err': 'Centrala nije prihvatila komandu.'})


async def h_events_range(request):
    """Događaji između from i to (unix vreme, to=0: do sada), najnoviji poslednji, najviše 500."""
    a = await request.json()
    t0, t1 = int(a.get('from') or 0), int(a.get('to') or 0) or int(time.time()) + 60
    return web.json_response({'events': [e for e in events if t0 <= e['t'] <= t1][-500:]})


async def h_events_clear(request):
    events.clear()
    save_events()
    bump()
    return web.json_response({'ok': True})


EVO_OVR = {'on': 'override_on', 'off': 'override_off', 'on_override': 'override_on', 'off_override': 'override_off', 'release': 'release'}


async def pgm_override(p, cmd):
    """EVO: PGM kao iz BabyWare statusnog prozora (broadcast „pgm_override“ ka centrali, adresa 0).
    Provereno 3.10.2026: ovo okida PGM na ovoj centrali (EVO48 v2.20), ali centrala obično ne pošalje potvrdu,
    pa se šalje jednom i ne čeka se (inače PGM ostaje uključen duže). PAI-jeva komanda 0x40 ovde ne radi.
    (Ranije je 'sub_command' bio van 'po' pa se paket nije ni sastavio, a u logu je pisalo „poslato“.)"""
    try:
        from paradox.hardware.evo import parsers as evp
        pid = int(p)
        if not 1 <= pid <= 16 or cmd not in EVO_OVR:
            return False
        data = evp.PGMBroadcastCommand.build({pid: EVO_OVR[cmd]})
        try:   # jednom; centrala izvrši, ali ne odgovara uvek
            reply = await alarm.send_wait(evp.BroadcastRequest, dict(po=dict(sub_command='pgm_override'), bus_address=0, data=data),
                                          reply_expected=0xA, retries=1, timeout=0.3)
        except asyncio.TimeoutError:
            reply = None
        log.info('PGM %s: %s (pgm_override) poslato, %s', pid, EVO_OVR[cmd], 'potvrđeno' if reply is not None else 'bez potvrde')
        return True
    except Exception as e:
        log.warning('PGM override %s %s: %s', p, cmd, e)
        return False


async def main():
    logging.getLogger('PAI').addHandler(SiteLog())
    app = web.Application()
    app.router.add_get('/state', h_state)
    app.router.add_get('/wait', h_wait)
    load_events()
    app.router.add_post('/partition', h_partition)
    app.router.add_post('/pgm', h_pgm)
    app.router.add_post('/zone', h_zone)
    app.router.add_post('/events/clear', h_events_clear)
    app.router.add_post('/events/range', h_events_range)
    runner = web.AppRunner(app, access_log=None)   # server pita na 1,5 s: bez zapisa svakog upita (/tmp je u RAM-u)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', PORT).start()   # samo lokalno: spolja se ide preko Teco.Pi servera
    log.info('Teco.Pi alarm most na 127.0.0.1:%d', PORT)
    await run_alarm()
    while True:   # nije podešen: API i dalje odgovara (stanje „off“)
        await asyncio.sleep(3600)


if __name__ == '__main__':
    asyncio.run(main())
