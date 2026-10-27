"""Generator signala (Alati → Elektronika): 2 kanala na GPIO pinovima proširenja osciloskopa (p3 = CH1, p4 = CH2).

Talas pravi pigpio (DMA, korak 1 µs) — procesor ne radi ništa dok signal ide.
  pravougaoni: 1 Hz – 100 kHz, duty 1–99 %
  trougaoni:   5 Hz – 1 kHz kao PWM od 10 kHz čiji duty raste pa pada; RC filter na izlazu (npr. 1 kΩ + 100 nF)
               od toga napravi trougao 0–3,3 V.
Oba kanala su u jednom talasu koji se ponavlja: dužina = period sporijeg kanala, pa se brži zaokruži na ceo broj
perioda u tom prozoru (frekvencija se malo pomeri — stvarna se prikazuje).
"""
CARRIER_US = 100            # trougaoni: PWM nosilac 10 kHz (100 koraka duty-ja po 1 µs)
MAX_PULSES = 11000          # pigpio: do 12000 impulsa u talasu
LIMITS = {'square': (1.0, 100000.0), 'tri': (5.0, 1000.0)}


class GenError(Exception):
    pass


def carrier(c):
    """Nosilac trougaonog (µs): 10 kHz, a za spore (ispod 20 Hz) ređi — do ~500 koraka po periodu (manje impulsa)."""
    return max(CARRIER_US, -(-round(1e6 / c['freq'] / 500) // CARRIER_US) * CARRIER_US)


def _window(chs):
    """Dužina talasa (µs): k perioda najsporijeg kanala, najmanje k za koje svaki kanal ima ceo broj perioda
    sa greškom ispod 1 % (i koje staje u broj impulsa); inače najbolje što staje."""
    import math
    tri = [carrier(c) for c in chs if c['wave'] == 'tri']
    step = math.lcm(*tri) if tri else 1   # prozor je ceo broj perioda svakog nosioca
    base = 1e6 / min(c['freq'] for c in chs)
    best = None
    for k in range(1, 101):
        w = round(k * base)
        w = max(step, round(w / step) * step)
        cost = sum(2 * (w // carrier(c)) if c['wave'] == 'tri' else 2 * max(1, round(w * c['freq'] / 1e6)) for c in chs)
        if cost > MAX_PULSES:
            break
        err = max(abs(max(1, round(w * c['freq'] / 1e6)) * 1e6 / w - c['freq']) / c['freq'] for c in chs)
        if best is None or err < best[0] - 1e-9:
            best = (err, w)
        if err < 0.01:
            break
    return best[1] if best else max(step, round(base / step) * step)


def build(chs):
    """chs: [{'gpio', 'wave', 'freq', 'duty'}] (samo uključeni). -> (impulsi [(on_maska, off_maska, kašnjenje_us)], stvarne frekvencije)."""
    if not chs:
        raise GenError('Nijedan kanal nije uključen.')
    for c in chs:
        lo, hi = LIMITS[c['wave']]
        if not lo <= c['freq'] <= hi:
            raise GenError('%s: %s talas ide od %g do %g Hz.' % (c['name'], 'Trougaoni' if c['wave'] == 'tri' else 'Pravougaoni', lo, hi))
    w = _window(chs)
    ev = {0: [0, 0]}   # vreme -> [on maska, off maska]

    def edge(t, gpio, high):
        e = ev.setdefault(t % w, [0, 0])
        e[0 if high else 1] |= 1 << gpio
    freqs = []
    for c in chs:
        n = max(1, round(w * c['freq'] / 1e6))   # ceo broj perioda u prozoru
        freqs.append(n * 1e6 / w)
        if c['wave'] == 'square':
            for k in range(n):
                t0, t1 = round(k * w / n), round((k + 1) * w / n)
                hi = min(t1 - t0 - 1, max(1, round((t1 - t0) * c['duty'] / 100)))
                edge(t0, c['gpio'], True)
                edge(t0 + hi, c['gpio'], False)
        else:
            cu = carrier(c)
            for j in range(w // cu):
                t0 = j * cu
                ph = ((t0 + cu / 2) * n / w) % 1.0
                hi = round((1 - abs(2 * ph - 1)) * cu)   # 0 -> vrh -> 0 tokom jednog perioda
                if hi <= 0:
                    edge(t0, c['gpio'], False)
                elif hi >= cu:
                    edge(t0, c['gpio'], True)
                else:
                    edge(t0, c['gpio'], True)
                    edge(t0 + hi, c['gpio'], False)
    ts = sorted(ev)
    if len(ts) > MAX_PULSES:
        raise GenError('Previše promena za jedan talas (%d). Smanji frekvenciju pravougaonog ili povećaj trougaonog.' % len(ts))
    pulses = [(ev[t][0], ev[t][1], (ts[i + 1] if i + 1 < len(ts) else w) - t) for i, t in enumerate(ts)]
    return pulses, freqs


class Gen:
    """Pokreće/zaustavlja talas preko pigpiod-a (servis radi samo dok generator radi)."""

    def __init__(self):
        self.pi = None
        self.gpios = []

    def start(self, chs):
        import pigpio
        pulses, freqs = build(chs)
        if self.pi is None or not self.pi.connected:
            self.pi = pigpio.pi()
            if not self.pi.connected:
                self.pi = None
                raise GenError('pigpiod ne radi.')
        pi = self.pi
        pi.wave_tx_stop()
        pi.wave_clear()
        self.gpios = [c['gpio'] for c in chs]
        for g in self.gpios:
            pi.set_mode(g, pigpio.OUTPUT)
            pi.write(g, 0)
        pi.wave_add_generic([pigpio.pulse(on, off, d) for on, off, d in pulses])
        wid = pi.wave_create()
        if wid < 0:
            raise GenError('pigpio nije napravio talas (%d).' % wid)
        pi.wave_send_repeat(wid)
        return freqs

    def stop(self):
        if self.pi is None:
            return
        import pigpio
        try:
            self.pi.wave_tx_stop()
            self.pi.wave_clear()
            for g in self.gpios:   # pinovi nazad kao ulazi (bez napona na izlazu)
                self.pi.write(g, 0)
                self.pi.set_mode(g, pigpio.INPUT)
            self.pi.stop()
        except Exception:
            pass
        self.pi, self.gpios = None, []
