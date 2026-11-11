#!/usr/bin/env bash
# Runs the GNOME Wayland suites against another GNOME Shell version, in a
# Fedora container (tests/gnome/Dockerfile), as an unprivileged user.
#   tests/gnome/e2e-gnome-in-container.sh fedora:41 [suite.py ...]
# Builds Fractile inside the container with the host's rustup toolchain
# (mounted read-only; it does not depend on the host's glibc). Evidence goes
# to tests/evidence/<suite>-gnome<major>.json.
set -euo pipefail
base=${1:?fedora image, e.g. fedora:41}; shift
root=$(cd "$(dirname "$0")/../.." && pwd)
tag="fractile-gnome-test:${base//:/-}"
docker image inspect "$tag" >/dev/null 2>&1 || docker build --build-arg BASE="$base" -t "$tag" "$root/tests/gnome"
suites=("$@"); [ ${#suites[@]} -gt 0 ] || suites=(tests/integration/e2e_gnome_wayland.py tests/integration/e2e_gnome_wayland_multimon.py
  tests/integration/e2e_gnome_wayland_displays.py tests/gui/settings_gnome_wayland.py tests/gui/tray_gnome_wayland.py)
toolchain=$(ls -d "${RUSTUP_HOME:-$HOME/.rustup}"/toolchains/stable-x86_64-unknown-linux-gnu)
mkdir -p "$root/tests/evidence"
docker run --rm -v "$root:/src:ro" -v "$toolchain:/toolchain:ro" -v "fractile-gnome-cache-${base//:/-}:/cache" \
  -v "$root/tests/evidence:/evidence" "$tag" bash -c "chown tester /cache /evidence && exec su tester -s /bin/bash -c 'bash /src/tests/gnome/container-user.sh ${suites[*]}'"
