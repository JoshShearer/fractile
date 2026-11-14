#!/usr/bin/env bash
# Real login / log-out / reboot test (SRS T10) in an Ubuntu 24.04 desktop VM.
#   tests/vm/vm-e2e.sh wayland|x11|plasma-x11|plasma-wayland [path/to/fractile.deb]
#     (plasma-wayland: Plasma 6 on Ubuntu 26.04; the others on Ubuntu 24.04)
#   tests/vm/vm-e2e.sh provision [session]  (only build the cached base image)
#   tests/vm/vm-e2e.sh soak [minutes]   (T27 soak in the VM, detached; default 1440)
# Needs docker and read/write access to /dev/kvm; no root. The first run
# downloads Ubuntu's cloud image (about 600 MB, checksum verified) and installs
# the desktop into a cached base image (docker volume fractile-vm-cache,
# about 8 GB). Each run then uses a throwaway copy-on-write disk.
set -euo pipefail
what=${1:?wayland, x11, plasma-x11, plasma-wayland, soak or provision}
root=$(cd "$(dirname "$0")/../.." && pwd)
if [[ $what != soak && $what != provision ]]; then
  deb=${2:-$(ls "$root"/packaging/build/fractile_*_amd64.deb | tail -1)}
  deb=$(realpath "$deb")
  case $deb in "$root"/*) ;; *) echo "the .deb must be inside the repository" >&2; exit 2 ;; esac
fi
[[ -r /dev/kvm && -w /dev/kvm ]] || { echo "no read/write access to /dev/kvm" >&2; exit 2; }
tag=fractile-vm-test:latest
docker image inspect "$tag" >/dev/null 2>&1 || docker build --build-arg UID="$(id -u)" -t "$tag" "$root/tests/vm"
mkdir -p "$root/tests/evidence/vm"
docker volume inspect fractile-vm-cache >/dev/null 2>&1 || docker volume create fractile-vm-cache >/dev/null
# The volume is created root-owned; hand it to the container user once.
docker run --rm -v fractile-vm-cache:/cache --user 0 "$tag" chown "$(id -u)" /cache
[[ $what != soak && $what != provision ]] && args=(--session "$what" --deb "/src/${deb#"$root"/}")
# Debug: keep the VM up for FRACTILE_VM_HOLD s at FRACTILE_VM_HOLD_AT (login|after-reboot).
[[ -n ${FRACTILE_VM_HOLD:-} ]] && args+=(--hold "$FRACTILE_VM_HOLD" --hold-at "${FRACTILE_VM_HOLD_AT:-login}")
# RS-18/T15: FRACTILE_VM_POWER_CUT=1 runs the power-cut scenario instead of reboot/log-out.
[[ -n ${FRACTILE_VM_POWER_CUT:-} ]] && args+=(--power-cut)
# FRACTILE_VM_PANEL=1 drives the panel with the VM keyboard and tablet instead.
[[ -n ${FRACTILE_VM_PANEL:-} ]] && args+=(--panel)
# PK-06: FRACTILE_VM_UPGRADE_FROM="old1.deb old2.deb" (inside the repository,
# oldest first) installs those, then upgrades to the .deb.
if [[ -n ${FRACTILE_VM_UPGRADE_FROM:-} ]]; then
  args+=(--upgrade-from)
  for d in $FRACTILE_VM_UPGRADE_FROM; do d=$(realpath "$d"); args+=("/src/${d#"$root"/}"); done
fi
[[ $what == provision ]] && args=(--session "${2:-wayland}" --deb /dev/null --provision-only)
if [[ $what == soak ]]; then
  # T27 in the VM: detached, so the run survives the terminal; follow it with
  # docker logs -f fractile-vm-soak or tests/evidence/vm/soak-progress.log.
  [[ -x $root/target/release/fractiled ]] || { echo "build first: cargo build --release" >&2; exit 2; }
  docker run -d --rm --name fractile-vm-soak --device /dev/kvm --group-add "$(stat -c %g /dev/kvm)" \
    -v "$root:/src:ro" -v fractile-vm-cache:/cache -v "$root/tests/evidence:/evidence" \
    "$tag" python3 /src/tests/vm/driver.py --soak-minutes "${2:-1440}" ${FRACTILE_VM_HOLD:+--hold "$FRACTILE_VM_HOLD" --hold-at fail}
  exit 0
fi
exec docker run --rm --name fractile-vm-${what} --device /dev/kvm --group-add "$(stat -c %g /dev/kvm)" \
  -v "$root:/src:ro" -v fractile-vm-cache:/cache -v "$root/tests/evidence:/evidence" \
  "$tag" python3 /src/tests/vm/driver.py "${args[@]}"
