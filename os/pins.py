"""Teco.Pi raspored GPIO pinova iz pins.toml (jedino mesto gde se pinovi definišu).
Stranica ih samo prikazuje; server ih čita odavde (releji, monitor, ulazi, SLIC, modem)."""
import re
import tomllib
from pathlib import Path

FILE = Path(__file__).with_name('pins.toml')
CONFIG_TXT = Path('/boot/firmware/config.txt')

# uloga releja u stanju servera -> pinovi iz fajla
RELAY_ROLES = {'av2': ('izvori', 'av1'), 'av3': ('izvori', 'av2'), 'av4': ('izvori', 'av3'), 'tuner': ('fm', 'releji'), 'monitor': ('monitor', 'napajanje')}


class Pins:
    def __init__(self):
        self.err = None
        try:
            self.d = tomllib.loads(FILE.read_text(encoding='utf-8'))
        except (OSError, tomllib.TOMLDecodeError) as e:
            self.d, self.err = {}, str(e)

    def get(self, sec, key, default=None, exp='exp1'):
        v = (self.d.get(sec) or {}).get(key, default)
        if isinstance(v, str) and re.fullmatch(r'p\d|tx|rx', v):   # "p3" -> pin sa izabranog proširenja
            v = (self.d.get(exp) or {}).get(v)
        elif isinstance(v, str) and not v:
            v = None
        return v

    def slic(self, exp='exp1'):
        return {'rm': self.get('slic', 'rm', exp=exp), 'fr': self.get('slic', 'fr', exp=exp), 'shk': self.get('slic', 'shk', exp=exp),
                'shk_low': bool(self.get('slic', 'shk_aktivno_nisko', True))}

    def pwrkey(self, exp='exp1'):
        return self.get('modem', 'pwrkey', '', exp=exp)

    @property
    def baud(self):
        return int(self.get('modem', 'brzina', 115200))

    def relay_pins(self, role):
        sec, key = RELAY_ROLES.get(role, (None, None))
        v = self.get(sec, key) if sec else None
        return [int(x) for x in (v if isinstance(v, list) else [v] if v is not None else [])]

    @property
    def relay_active_high(self):
        return not self.get('releji', 'aktivno_nisko', False)

    def table(self):
        """[(pin, funkcija, grupa)] za prikaz, sortirano po pinu; + upozorenja (dupli pin, config.txt)."""
        rows, warn = [], []
        names = {
            ('i2c', 'sda'): 'I2C SDA', ('i2c', 'scl'): 'I2C SCL', ('senzori', 'dht22'): 'DHT22 (temperatura, vlaga)',
            ('senzori', 'ir'): 'IR prijemnik (TSOP)', ('monitor', 'napajanje'): 'Monitor: napajanje (rele)', ('monitor', 'source'): 'Monitor: dugme SOURCE',
            ('monitor', 'levo'): 'Monitor: dugme levo', ('monitor', 'desno'): 'Monitor: dugme desno',
            ('ulazi', 'struja'): 'Ulaz: ima struje', ('ulazi', 'baterija'): 'Ulaz: na bateriji'}
        grp = {'i2c': 'I2C', 'senzori': 'Senzori', 'izvori': 'Izvori', 'fm': 'FM', 'monitor': 'Monitor', 'ulazi': 'Ulazi', 'exp1': 'EXP1', 'exp2': 'EXP2'}
        use = {}   # pin sa proširenja -> ko ga koristi
        for sec, keys in (('slic', ('rm', 'fr', 'shk')), ('modem', ('pwrkey',))):
            for k in keys:
                ref = (self.d.get(sec) or {}).get(k)
                if ref:
                    use[ref] = '%s %s' % ('SLIC' if sec == 'slic' else 'Modem', k.upper())
        use.update(tx='UART TX (modem)', rx='UART RX (modem)')
        for sec, vals in self.d.items():
            if sec not in grp:
                continue
            for k, v in vals.items():
                if not isinstance(v, (int, list)) or isinstance(v, bool):
                    continue
                for i, p in enumerate(v if isinstance(v, list) else [v]):
                    if sec == 'izvori':
                        n = 'Izvor %s: rele %d' % (k.upper(), i + 1)
                    elif sec == 'fm':
                        n = 'FM radio: rele %d' % (i + 1)
                    elif sec in ('exp1', 'exp2'):
                        n = '%s %s%s' % (grp[sec], k.upper(), (': ' + use[k]) if k in use else '')
                    else:
                        n = names.get((sec, k), '%s %s' % (grp[sec], k))
                    rows.append({'pin': int(p), 'n': n, 'g': grp[sec]})
        seen = {}
        for r in rows:
            if r['pin'] in seen and not (r['g'] == 'EXP2' and r['pin'] in (self.get('exp1', 'tx'), self.get('exp1', 'rx'))):   # UART dele oba proširenja
                warn.append('GPIO%d je dva puta: %s i %s' % (r['pin'], seen[r['pin']], r['n']))
            seen[r['pin']] = r['n']
            if not 2 <= r['pin'] <= 27:
                warn.append('GPIO%d ne postoji na Pi-ju' % r['pin'])
        try:   # kernel (config.txt) mora da se slaže sa fajlom za IR i DHT22
            ct = CONFIG_TXT.read_text()
            for key, rx, label in (('ir', r'^dtoverlay=gpio-ir,gpio_pin=(\d+)', 'IR'), ('dht22', r'^dtoverlay=dht11,gpiopin=(\d+)', 'DHT22')):
                m = re.search(rx, ct, re.M)
                want = self.get('senzori', key)
                if m and want is not None and int(m.group(1)) != int(want):
                    warn.append('%s: config.txt kaže GPIO%s, pins.toml GPIO%s' % (label, m.group(1), want))
        except OSError:
            pass
        if self.err:
            warn.insert(0, 'pins.toml se ne čita: ' + self.err)
        return sorted(rows, key=lambda r: r['pin']), warn


PINS = Pins()


def reload():
    global PINS
    PINS = Pins()
    return PINS
