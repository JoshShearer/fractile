#!/usr/bin/env bash
# Real login / log-out / reboot test (SRS T10) in an Ubuntu 24.04 desktop VM.
#   tests/vm/vm-e2e.sh wayland|x11 [path/to/fractile.deb]
#   tests/vm/vm-e2e.sh provision        (only build the cached base image)
# Needs docker and read/write access to /dev/kvm; no root. The first run
# downloads Ubuntu's cloud image (about 600 MB, checksum verified) and installs
# the desktop into a cached base image (docker volume fractile-vm-cache,
# about 8 GB). Each run then uses a throwaway copy-on-write disk.
set -euo pipefail
what=${1:?wayland, x11 or provision}
root=$(cd "$(dirname "$0")/../.." && pwd)
deb=${2:-$(ls "$root"/packaging/build/fractile_*_amd64.deb | tail -1)}
deb=$(realpath "$deb")
case $deb in "$root"/*) ;; *) echo "the .deb must be inside the repository" >&2; exit 2 ;; esac
[[ -r /dev/kvm && -w /dev/kvm ]] || { echo "no read/write access to /dev/kvm" >&2; exit 2; }
tag=fractile-vm-test:latest
docker image inspect "$tag" >/dev/null 2>&1 || docker build --build-arg UID="$(id -u)" -t "$tag" "$root/tests/vm"
mkdir -p "$root/tests/evidence/vm"
docker volume inspect fractile-vm-cache >/dev/null 2>&1 || docker volume create fractile-vm-cache >/dev/null
# The volume is created root-owned; hand it to the container user once.
docker run --rm -v fractile-vm-cache:/cache --user 0 "$tag" chown "$(id -u)" /cache
args=(--session "$what" --deb "/src/${deb#"$root"/}")
# Debug: keep the VM up for FRACTILE_VM_HOLD s at FRACTILE_VM_HOLD_AT (login|after-reboot).
[[ -n ${FRACTILE_VM_HOLD:-} ]] && args+=(--hold "$FRACTILE_VM_HOLD" --hold-at "${FRACTILE_VM_HOLD_AT:-login}")
[[ $what == provision ]] && args=(--session wayland --deb /dev/null --provision-only)
exec docker run --rm --name fractile-vm-${what} --device /dev/kvm --group-add "$(stat -c %g /dev/kvm)" \
  -v "$root:/src:ro" -v fractile-vm-cache:/cache -v "$root/tests/evidence:/evidence" \
  "$tag" python3 /src/tests/vm/driver.py "${args[@]}"
