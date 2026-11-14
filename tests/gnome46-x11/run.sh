#!/usr/bin/env bash
# Runs the X11 suites under GNOME Shell 46 (X11 session, real mutter WM) on
# Xvfb in an Ubuntu 24.04 container, as the host user, so nothing opens on
# the host's display. Needs the debug build (cargo build).
#   tests/gnome46-x11/run.sh [suite.py ...]
# Default suites: X11 E2E, GUI, Settings, accessibility.
# Evidence: tests/evidence/<suite>-gnome46-x11.json.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
docker image inspect fractile-gnome46-x11-test >/dev/null 2>&1 || docker build -t fractile-gnome46-x11-test "$root/tests/gnome46-x11"
suites=("$@"); [ ${#suites[@]} -gt 0 ] || suites=(tests/integration/e2e_x11.py tests/gui/gui_x11.py tests/gui/settings_x11.py tests/gui/a11y_x11.py)
# As root first: GNOME Shell 46 aborts at startup without a system bus, so
# start one; package installs leave /run/systemd/seats behind, which tells
# the shell that logind runs (it does not here), so move it aside. Then run
# as a user with the host's uid, so the evidence files stay the host user's.
uid=$(id -u); gid=$(id -g)
docker run --rm -e UID_="$uid" -e GID_="$gid" ${FRACTILE_ORCA_LOG:+-e FRACTILE_ORCA_LOG=/evidence/$(basename "$FRACTILE_ORCA_LOG")} -v "$root:/src:ro" -v "$root/tests/evidence:/evidence" \
  fractile-gnome46-x11-test bash -c '
mv /run/systemd/seats /tmp/seats.unused 2>/dev/null || true
mkdir -p /run/dbus && dbus-daemon --system --fork
groupadd -o -g "$GID_" t 2>/dev/null; useradd -o -u "$UID_" -g "$GID_" -m t
exec su t -s /bin/bash -c "bash /src/tests/gnome46-x11/user.sh $*"' _ "${suites[@]}"
