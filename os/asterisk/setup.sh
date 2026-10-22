#!/bin/sh
# Teco.Pi: osnovna podešavanja Asteriska (jednom, kao root). Naš deo generiše server u /etc/asterisk/teco/.
set -e
A=/etc/asterisk
U=${1:-dmilosavljevic}
[ -d $A.orig ] || cp -a $A $A.orig            # original paketa ostaje sačuvan
install -d -o "$U" -g asterisk -m 2775 $A/teco /var/lib/asterisk/sounds/teco /var/lib/asterisk/sounds/teco/ringback
usermod -aG asterisk "$U"
install -m 640 -o asterisk -g asterisk "$(dirname "$0")/modules.conf" $A/modules.conf
w() { printf '%s\n' "$2" > "$A/$1"; chown asterisk:asterisk "$A/$1"; chmod 640 "$A/$1"; }
w http.conf '[general]
enabled=yes
bindaddr=127.0.0.1
bindport=8088'
w ari.conf '[general]
enabled=yes
pretty=no
allowed_origins=
#tryinclude teco/ari.conf'
w pjsip.conf '#tryinclude teco/pjsip.conf'
w extensions.conf '[general]
static=yes
writeprotect=yes
[globals]
TECO_SLIC=0
#tryinclude teco/extensions.conf'
w voicemail.conf '[general]
format=wav
attach=no
maxmsg=100
maxsecs=180
minsecs=2
maxsilence=0
silencethreshold=128
maxlogins=3
skipms=3000
review=no
operator=no
#tryinclude teco/voicemail.conf'
w musiconhold.conf '[general]
[ringback]
mode=files
directory=/var/lib/asterisk/sounds/teco/ringback
sort=alpha'
w rtp.conf '[general]
rtpstart=10000
rtpend=10200'
w logger.conf '[general]
[logfiles]
messages.log => notice,warning,error'
# govorna pošta: grupa asterisk (server) može da čita i briše
chgrp asterisk /var/spool/asterisk && chmod g+rx /var/spool/asterisk
install -d -o asterisk -g asterisk -m 2770 /var/spool/asterisk/voicemail
chmod -R g+rwX /var/spool/asterisk/voicemail
mkdir -p /etc/systemd/system/asterisk.service.d
printf '[Service]\nUMask=0007\n' > /etc/systemd/system/asterisk.service.d/teco.conf
systemctl daemon-reload
echo ok
