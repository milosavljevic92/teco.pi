"""Email (Ekstenzije): slanje obaveštenja preko SMTP-a — Microsoft 365 / Exchange (SMTP AUTH), Gmail (lozinka za aplikaciju)
ili bilo koji drugi SMTP server. Blokirajuće (smtplib) — server ga zove u executor-u."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

PRESETS = {'m365': {'host': 'smtp.office365.com', 'port': 587, 'sec': 'starttls'},
           'gmail': {'host': 'smtp.gmail.com', 'port': 587, 'sec': 'starttls'}}


def send(cfg, to, subject, body, timeout=20):
    """cfg: host, port, sec (starttls|ssl|none), user, pw, from, from_name. Vraća None ili tekst greške."""
    msg = EmailMessage()
    msg['From'] = formataddr((cfg.get('from_name') or 'Teco.Pi', cfg['from']))
    msg['To'] = ', '.join(to)
    msg['Subject'] = subject
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain=cfg['from'].split('@')[-1])
    msg.set_content(body)
    ctx = ssl.create_default_context()
    try:
        if cfg['sec'] == 'ssl':
            s = smtplib.SMTP_SSL(cfg['host'], cfg['port'], timeout=timeout, context=ctx)
        else:
            s = smtplib.SMTP(cfg['host'], cfg['port'], timeout=timeout)
        with s:
            s.ehlo()
            if cfg['sec'] == 'starttls':
                s.starttls(context=ctx)
                s.ehlo()
            if cfg.get('user'):
                s.login(cfg['user'], cfg.get('pw') or '')
            s.send_message(msg)
        return None
    except smtplib.SMTPAuthenticationError as e:
        txt = (e.smtp_error or b'').decode(errors='replace')
        if 'SmtpClientAuthentication is disabled' in txt or '5.7.139' in txt:
            return 'Exchange: SMTP AUTH je isključen za ovaj nalog (uključi ga u Microsoft 365 admin centru → korisnik → Mail → Authenticated SMTP)'
        return 'Prijava nije uspela (%d): %s' % (e.smtp_code, txt[:160])
    except smtplib.SMTPRecipientsRefused:
        return 'Server je odbio primaoca'
    except smtplib.SMTPSenderRefused as e:
        return 'Server je odbio pošiljaoca %s (adresa mora biti tvoja / dozvoljena): %s' % (cfg['from'], (e.smtp_error or b'').decode(errors='replace')[:120])
    except smtplib.SMTPException as e:
        return 'SMTP: %s' % str(e)[:180]
    except ssl.SSLError as e:
        return 'TLS: %s (proveri port i zaštitu)' % (e.reason or e)
    except OSError as e:
        return 'Nema veze sa %s:%s (%s)' % (cfg['host'], cfg['port'], e.strerror or e.__class__.__name__)
