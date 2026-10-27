#!/bin/bash
# Teco.Pi: osnovno ojačavanje OS-a (može više puta). Pokreće se kao korisnik sa sudo pravima.
set -e
D=$(dirname "$(readlink -f "$0")")

# mrežni parametri kernela: bez ICMP preusmeravanja, provera izvorne adrese (2 = labavo, zbog VPN-a), skrivene adrese kernela
sudo tee /etc/sysctl.d/90-teco.conf >/dev/null <<'E'
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.default.rp_filter = 2
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.log_martians = 0
kernel.kptr_restrict = 1
kernel.dmesg_restrict = 1
E
sudo sysctl -q --system

# SSH: bez root prijave i X11, manje pokušaja
sudo tee /etc/ssh/sshd_config.d/90-teco.conf >/dev/null <<'E'
PermitRootLogin no
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
E
sudo sshd -t && sudo systemctl reload ssh

# zaostali sistemski nalog bez paketa (piaware, FlightAware): ukloni
if id piaware >/dev/null 2>&1 && ! dpkg -s piaware >/dev/null 2>&1; then
  sudo userdel piaware
  getent group piaware >/dev/null && sudo groupdel piaware || true
fi

# IPv6 se ne koristi: isključen u kernelu (cmdline, od sledećeg butovanja), odmah preko sysctl-a i u NetworkManager-u
CL=/boot/firmware/cmdline.txt
grep -q 'ipv6.disable=1' $CL || sudo sed -i '1 s/$/ ipv6.disable=1/' $CL
printf 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\nnet.ipv6.conf.lo.disable_ipv6 = 1\n' | sudo tee /etc/sysctl.d/91-teco-noipv6.conf >/dev/null
sudo sysctl -q -p /etc/sysctl.d/91-teco-noipv6.conf 2>/dev/null || true
nmcli -t -f NAME,TYPE con show | while IFS=: read -r n ty; do
  case "$ty" in 802-11-wireless|802-3-ethernet) sudo nmcli con mod "$n" ipv6.method disabled ;; esac
done
# SSH i Samba: samo IPv4
echo 'AddressFamily inet' | sudo tee /etc/ssh/sshd_config.d/91-teco-inet.conf >/dev/null
sudo sshd -t && sudo systemctl reload ssh

# pouzdanost: hardverski watchdog (zaglavljen sistem se sam restartuje)
sudo mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' | sudo tee /etc/systemd/system.conf.d/90-teco-watchdog.conf >/dev/null
sudo systemctl daemon-reexec

# zakrpe same: Raspbian nema poseban security repo, pa sve stabilne ispravke (bookworm) — bez kernela i firmware-a
# (to menja butovanje; ide ručno) i bez automatskog restarta
sudo DEBIAN_FRONTEND=noninteractive apt-get -y -q install unattended-upgrades >/dev/null
sudo tee /etc/apt/apt.conf.d/52teco-unattended >/dev/null <<'E'
Unattended-Upgrade::Origins-Pattern { "origin=Raspbian,codename=${distro_codename},label=Raspbian"; };
Unattended-Upgrade::Package-Blacklist { "linux-image-"; "linux-headers-"; "raspi-firmware"; "rpi-eeprom"; "raspberrypi-"; };
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
E
for u in apt-daily.service apt-daily-upgrade.service; do sudo systemctl unmask $u >/dev/null 2>&1 || true; done
sudo systemctl enable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null

# firewall
[ -f "$D/nftables.conf" ] && sudo cp "$D/nftables.conf" /etc/nftables.conf && sudo systemctl enable --now nftables && sudo nft -f /etc/nftables.conf
echo ok
