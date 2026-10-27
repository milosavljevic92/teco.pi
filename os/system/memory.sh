#!/bin/bash
# Teco.Pi: manje memorije kod procesa koji dugo rade (mpv, server). glibc pravi po jednu malloc „arenu“
# za svaku nit i ne vraća oslobođenu memoriju — posle par dana mpv zauzme 2x više nego posle pokretanja.
set -e
sudo mkdir -p /etc/systemd/system/teco-web.service.d
# numpy (splash, telefonska linija) vuče OpenBLAS koji pravi bafer za svako jezgro: jedna nit je dovoljna
printf '[Service]\nEnvironment=MALLOC_ARENA_MAX=2\nEnvironment=OPENBLAS_NUM_THREADS=1\n' | sudo tee /etc/systemd/system/teco-web.service.d/90-memory.conf >/dev/null
U=$HOME/.config/systemd/user
for s in teco-player teco-cast teco-alarm; do
  mkdir -p "$U/$s.service.d"
  printf '[Service]\nEnvironment=MALLOC_ARENA_MAX=2\n' > "$U/$s.service.d/90-memory.conf"
done
# Cast (Node): mali V8 heap — samo prima komande sa telefona, podrazumevani heap je za velike aplikacije
printf '[Service]\nEnvironment=NODE_OPTIONS="--max-old-space-size=48 --max-semi-space-size=1"\n' > "$U/teco-cast.service.d/91-heap.conf"
sudo systemctl daemon-reload
systemctl --user daemon-reload
echo ok
