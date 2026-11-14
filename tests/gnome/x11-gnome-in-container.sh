#!/usr/bin/env bash
# Runs the X11 suites under GNOME Shell's X11 session (real mutter WM) in a
# Fedora container, on Xvfb instead of Xephyr, so nothing opens on the
# host's display. As an unprivileged user.
#   tests/gnome/x11-gnome-in-container.sh [fedora:39] [suite.py ...]
# Default suites: the X11 E2E, the GUI suite and the Settings suite.
# Evidence: tests/evidence/<suite>-gnome<major>-x11.json.
set -euo pipefail
base=${1:-fedora:39}; shift || true
root=$(cd "$(dirname "$0")/../.." && pwd)
tag="fractile-gnome-test:${base//:/-}"
docker image inspect "$tag" >/dev/null 2>&1 || docker build --build-arg BASE="$base" -t "$tag" "$root/tests/gnome"
suites=("$@"); [ ${#suites[@]} -gt 0 ] || suites=(tests/integration/e2e_x11.py tests/gui/gui_x11.py tests/gui/settings_x11.py)
toolchain=$(ls -d "${RUSTUP_HOME:-$HOME/.rustup}"/toolchains/stable-x86_64-unknown-linux-gnu)
mkdir -p "$root/tests/evidence"
# The image is for the Wayland suites; the X11 server and tools come at run time
# (dbus-x11: GTK4 test windows get their application id only on a session bus).
setup="dnf -q -y install xorg-x11-server-Xvfb xdotool xprop xrandr xwininfo dbus-x11 python3-xlib glibc-langpack-ar >/tmp/dnf.log 2>&1 || { tail -20 /tmp/dnf.log; exit 1; }
rm -rf /run/systemd/seats && mkdir -p /run/dbus && dbus-daemon --system --fork"
docker run --rm -v "$root:/src:ro" -v "$toolchain:/toolchain:ro" -v "fractile-gnome-cache-${base//:/-}:/cache" \
  -v "$root/tests/evidence:/evidence" "$tag" bash -c "$setup && chown tester /cache /evidence && exec su tester -s /bin/bash -c '
set -uo pipefail
export PATH=/toolchain/bin:\$PATH CARGO_HOME=/cache/cargo CARGO_TARGET_DIR=/cache/target
cd /src && cargo build --locked -q -p fractile-daemon -p fractile-cli 2>&1 | tail -5
export XDG_RUNTIME_DIR=/tmp/rt FRACTILE_BIN_DIR=/cache/target/debug FRACTILE_TEST_XSERVER=xvfb PYTHONDONTWRITEBYTECODE=1
export FRACTILE_GUI_SHOTS=/tmp/shots FRACTILE_SHOT_DIR=/tmp/shots
mkdir -m700 -p \$XDG_RUNTIME_DIR /tmp/shots
major=\$(gnome-shell --version | grep -o \"[0-9]*\" | head -1)
echo \"== \$(gnome-shell --version) X11 on Xvfb\"
rc=0
for s in ${suites[*]}; do
  n=\$(basename \$s .py | tr _ -)
  echo \"== \$n\"
  python3 /src/\$s --evidence /evidence/\$n-gnome\$major-x11.json || rc=1
done
exit \$rc'"
