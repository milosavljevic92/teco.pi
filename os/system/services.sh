#!/bin/bash
# Teco.Pi: sistemski servisi iz ovog foldera (teco-*.service) -> /etc/systemd/system, uključeni i pokrenuti.
set -e
D=$(dirname "$(readlink -f "$0")")
for f in "$D"/teco-*.service; do
  n=$(basename "$f")
  sudo install -m 644 "$f" /etc/systemd/system/"$n"
done
sudo systemctl daemon-reload
for f in "$D"/teco-*.service; do sudo systemctl enable "$(basename "$f")" >/dev/null 2>&1; sudo systemctl restart "$(basename "$f")"; done
echo ok
