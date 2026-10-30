#!/bin/bash
# Teco.Pi: CUPS server za štampu — svi uređaji u kućnoj mreži (i preko VPN-a) štampaju preko Pi-ja.
# Štampači se dodaju bez drajvera (IPP Everywhere / AirPrint); Pi ih deli dalje (mDNS: iPhone, Android, Mac, Windows).
# Administracija CUPS-a samo sa samog Pi-ja (Teco.Pi stranica je ta koja dodaje/uklanja štampače).
set -e
# printer-driver-splix: Samsung/Xerox štampači koji primaju samo QPDL (npr. Xerox Phaser 3020) — bez njega ne štampaju
# avahi-utils: provera šta Pi oglašava u mreži (AirPrint)
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends cups cups-filters cups-ipp-utils printer-driver-splix avahi-utils
sudo usermod -aG lpadmin "$USER"
sudo tee /etc/cups/cupsd.conf >/dev/null <<'EOF'
# generiše Teco.Pi (system/cups.sh)
LogLevel warn
MaxLogSize 1m
MaxJobs 100
PreserveJobHistory 1h
Port 631
Listen /run/cups/cups.sock
Browsing On
BrowseLocalProtocols dnssd
DefaultAuthType Basic
WebInterface No
DefaultShared Yes
<Location />
  Order allow,deny
  Allow @LOCAL
  Allow 10.10.0.0/16
</Location>
<Location /admin>
  Order allow,deny
  Allow localhost
</Location>
<Location /admin/conf>
  AuthType Default
  Require user @SYSTEM
  Order allow,deny
  Allow localhost
</Location>
<Policy default>
  JobPrivateAccess default
  JobPrivateValues default
  SubscriptionPrivateAccess default
  SubscriptionPrivateValues default
  <Limit Create-Job Print-Job Print-URI Validate-Job>
    Order deny,allow
  </Limit>
  <Limit Send-Document Send-URI Hold-Job Release-Job Restart-Job Purge-Jobs Set-Job-Attributes Create-Job-Subscription Renew-Subscription Cancel-Subscription Get-Notifications Reprocess-Job Cancel-Current-Job Suspend-Current-Job Resume-Job Cancel-My-Jobs Close-Job CUPS-Move-Job CUPS-Get-Document>
    Require user @OWNER @SYSTEM
    Order deny,allow
  </Limit>
  <Limit CUPS-Add-Modify-Printer CUPS-Delete-Printer CUPS-Add-Modify-Class CUPS-Delete-Class CUPS-Set-Default CUPS-Get-Devices>
    AuthType Default
    Require user @SYSTEM
    Order deny,allow
  </Limit>
  <Limit Pause-Printer Resume-Printer Enable-Printer Disable-Printer Pause-Printer-After-Current-Job Hold-New-Jobs Release-Held-New-Jobs Deactivate-Printer Activate-Printer Restart-Printer Shutdown-Printer Startup-Printer Promote-Job Schedule-Job-After Cancel-Jobs CUPS-Accept-Jobs CUPS-Reject-Jobs>
    AuthType Default
    Require user @SYSTEM
    Order deny,allow
  </Limit>
  <Limit Cancel-Job CUPS-Authenticate-Job>
    Require user @OWNER @SYSTEM
    Order deny,allow
  </Limit>
  <Limit All>
    Order deny,allow
  </Limit>
</Policy>
EOF
# firewall: IPP (631) iz kućne mreže i VPN-a
sudo cp "$(dirname "$0")/nftables.conf" /etc/nftables.conf
sudo nft -f /etc/nftables.conf
sudo systemctl enable --now cups
sudo systemctl restart cups
echo ok
