#!/bin/bash
# Teco.Pi: sat preko chrony (umesto systemd-timesyncd) — Pi uzima vreme sa NTP servera (Srbija ili Cloudflare) i,
# kad je uključeno u Ekstenzije → NTP vreme, deli ga uređajima u kućnoj mreži i preko VPN-a (UDP 123).
# Izvore i deljenje piše Teco.Pi u /etc/chrony/conf.d/teco.conf; podrazumevani Debian pool se ne koristi.
set -e
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends chrony
sudo sed -i 's/^\(pool .*\)$/# \1   # Teco.Pi: izvori su u conf.d\/teco.conf/' /etc/chrony/chrony.conf
[ -f /etc/chrony/conf.d/teco.conf ] || printf '# generiše Teco.Pi\npool 0.rs.pool.ntp.org iburst\npool 1.rs.pool.ntp.org iburst\n' | sudo tee /etc/chrony/conf.d/teco.conf >/dev/null
sudo cp "$(dirname "$0")/nftables.conf" /etc/nftables.conf
sudo nft -f /etc/nftables.conf
sudo systemctl enable --now chrony
sudo systemctl restart chrony
echo ok
