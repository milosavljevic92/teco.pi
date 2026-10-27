"""Teco.Pi tajmer na ekranu (sloj preko plejera mpv): odbrojavanje (krug), štoperica (sa krugovima) ili alarm (HH:MM).
Kad zazvoni (isteklo / alarm), treperi „ISTEKLO“ ili „ALARM“. Stanje drži server u JSON fajlu; crta se kad se promeni prikaz.

python3 timer.py <data/tool-timer.json>
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CFG = Path(sys.argv[1] if len(sys.argv) > 1 else 'tool-timer.json')
FONTB = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONTM = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
BG, ACC, TEXT, MUTED, DONE, SW, AL = (6, 9, 13), (249, 115, 22), (245, 247, 250), (100, 116, 139), (220, 38, 38), (14, 165, 233), (168, 85, 247)

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def F(path, s):
    return ImageFont.truetype(path, max(8, int(s)))


def left_of(cfg):
    if cfg.get('run'):
        return max(0.0, float(cfg.get('end') or 0) - time.time())
    return max(0.0, float(cfg.get('left') or 0))


def sw_of(cfg):
    return float(cfg.get('sw_acc') or 0) + (time.time() - float(cfg.get('sw_start') or 0) if cfg.get('sw_run') else 0)


def fmt(sec):
    s = int(sec + 0.999)   # 4,2 s preostalo -> „0:05“ (kao kuhinjski tajmer)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return '%d:%02d:%02d' % (h, m, s) if h else '%d:%02d' % (m, s)


def fmt_sw(sec):
    h, m, s, t = int(sec // 3600), int(sec // 60 % 60), int(sec % 60), int(sec * 10 % 10)
    return ('%d:%02d:%02d' % (h, m, s) if h else '%d:%02d' % (m, s)) + '.%d' % t


def center(d, cx, y, s, f, col):
    d.text((cx - d.textlength(s, font=f) / 2, y), s, font=f, fill=col)


def render(cfg, w, h, blink):
    img = Image.new('RGB', (w, h), BG)
    d = ImageDraw.Draw(img)
    mode = cfg.get('mode') or 'cd'
    ring = bool(cfg.get('ring'))
    title = {'cd': 'ODBROJAVANJE', 'sw': 'ŠTOPERICA', 'al': 'ALARM'}[mode]
    d.text((w * 0.04, h * 0.04), title, font=F(FONTB, h / 24), fill={'cd': ACC, 'sw': SW, 'al': AL}[mode])
    if cfg.get('label') and mode != 'sw':
        d.text((w * 0.04, h * 0.1), str(cfg['label']), font=F(FONTB, h / 14), fill=TEXT)
    cx, cy = w / 2, h * 0.54
    if ring:   # zvoni: preko celog ekrana
        if blink:
            d.rectangle([0, 0, w, h], fill=(60, 8, 8))
        s = 'ALARM' if cfg.get('ring_kind') == 'al' else 'ISTEKLO'
        center(d, cx, cy - h * 0.16, s, F(FONTB, h * 0.22), DONE if not blink else TEXT)
        sub = (cfg.get('al_at') if cfg.get('ring_kind') == 'al' else '') + ('  ·  ' + cfg['label'] if cfg.get('label') else '')
        if sub.strip(' ·'):
            center(d, cx, cy + h * 0.12, sub.strip(' ·'), F(FONTB, h / 12), TEXT)
        return img
    if mode == 'sw':
        el = sw_of(cfg)
        center(d, cx, cy - h * 0.2, fmt_sw(el), F(FONTM, h * 0.2), TEXT if cfg.get('sw_run') else MUTED)
        laps = cfg.get('laps') or []
        lf = F(FONTM, h / 24)
        for i, lp in enumerate(reversed(laps[-5:])):
            n = len(laps) - i
            prev = laps[n - 2] if n >= 2 else 0
            s = 'krug %d   %s   +%s' % (n, fmt_sw(lp), fmt_sw(lp - prev))
            center(d, cx, cy + h * 0.06 + i * h * 0.065, s, lf, SW if i == 0 else MUTED)
        return img
    if mode == 'al':
        at = cfg.get('al_at') or '07:00'
        on = cfg.get('al_on')
        center(d, cx, cy - h * 0.2, at, F(FONTM, h * 0.24), TEXT if on else MUTED)
        if on:
            hh, mm = map(int, at.split(':'))
            lt = time.localtime()
            now_m = lt.tm_hour * 60 + lt.tm_min
            dm = (hh * 60 + mm - now_m) % 1440 or 1440
            s = 'zvoni za %d h %02d min' % (dm // 60, dm % 60) if dm >= 60 else 'zvoni za %d min' % dm
        else:
            s = 'alarm je isključen'
        center(d, cx, cy + h * 0.12, s, F(FONTB, h / 14), AL if on else MUTED)
        center(d, cx, cy + h * 0.24, time.strftime('sada %H:%M'), F(FONTB, h / 22), MUTED)
        return img
    left, dur = left_of(cfg), max(1, int(cfg.get('dur') or 1))
    r = h * 0.36
    box = [cx - r, cy - r, cx + r, cy + r]
    wd = int(h * 0.035)
    d.ellipse(box, outline=(30, 41, 59), width=wd)
    if left / dur > 0:
        d.arc(box, -90, -90 + 360 * min(1, left / dur), fill=ACC if cfg.get('run') else MUTED, width=wd)
    s = fmt(left)
    f = F(FONTM, r * (0.62 if len(s) <= 5 else 0.36))   # h:mm:ss je širi
    center(d, cx, cy - f.size * 0.62, s, f, TEXT if cfg.get('run') else MUTED)
    sub = '' if cfg.get('run') else ('pauza' if left < dur else 'spreman')
    if sub:
        center(d, cx, cy + r * 0.42, sub, F(FONTB, h / 26), MUTED)
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    from screen import Screen
    scr = Screen('timer')
    w, h = scr.size()
    mt, cfg, key, last = object(), {}, None, 0
    while running:
        try:
            m = CFG.stat().st_mtime
        except OSError:
            m = None
        if m != mt:
            mt = m
            try:
                cfg = json.loads(CFG.read_text())
            except (OSError, ValueError):
                cfg = {}
        mode, ring = cfg.get('mode') or 'cd', bool(cfg.get('ring'))
        blink = ring and int(time.time() * 2) % 2 == 0
        # ključ prikaza: menja se kad treba precrtati (štoperica: desetinke, ostalo: sekunde/minute)
        val = fmt_sw(sw_of(cfg)) if mode == 'sw' else time.strftime('%H:%M') if mode == 'al' else fmt(left_of(cfg))
        k = (mode, ring, blink, val, json.dumps(cfg, sort_keys=True))
        if k != key or time.time() - last > 2:
            key = k
            img = render(cfg, w, h, blink)
            if os.environ.get('TIMER_SNAP'):
                img.save(os.environ['TIMER_SNAP'])
                return
            scr.show(img.convert('RGBA').tobytes('raw', 'BGRA'), w, h)
            last = time.time()
        time.sleep(0.1)
    scr.close()


if __name__ == '__main__':
    main()
