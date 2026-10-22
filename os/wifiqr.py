"""Teco.Pi Wi-Fi za goste: QR kod koji telefon kamerom prepozna i odmah se poveže (format WIFI:T:WPA;S:…;P:…;;)."""


def _esc(s):
    return ''.join('\\' + c if c in '\\;,:"' else c for c in s)


def payload(cfg):
    sec = cfg.get('sec') or 'WPA'
    p = 'WIFI:T:%s;S:%s;' % ('nopass' if sec == 'nopass' else sec, _esc(cfg.get('ssid') or ''))
    if sec != 'nopass':
        p += 'P:%s;' % _esc(cfg.get('pw') or '')
    if cfg.get('hidden'):
        p += 'H:true;'
    return p + ';'


def image(cfg, px=600):
    """PIL slika QR koda (crno na belom, sa tihom zonom), kvadrat px×px."""
    import qrcode
    from PIL import Image
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=10)
    q.add_data(payload(cfg))
    q.make(fit=True)
    img = q.make_image(fill_color='black', back_color='white').get_image().convert('RGB')
    return img.resize((px, px), Image.NEAREST)
