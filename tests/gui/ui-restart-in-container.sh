#!/usr/bin/env bash
# Regression test: after a service restart only one UI stays attached.
# Sequence from a real Pop!_OS session: the tray-only UI leaves (no tray on
# the desktop), the user opens Fractile from the menu (that UI stays), then
# the service restarts and starts a tray-only UI again before the menu UI
# reconnects. Both answered the panel shortcut (two panels per monitor).
# Needs the debug build (cargo build) and the kwin-x11 test image.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
docker run --rm --user "$(id -u):$(id -g)" -v "$root:/src:ro" fractile-kwin-x11-test:latest bash -euo pipefail -c '
mkdir -p /tmp/t/home /tmp/r; chmod 700 /tmp/r
export HOME=/tmp/t/home XDG_RUNTIME_DIR=/tmp/r XDG_CONFIG_HOME=/tmp/t/home/.config XDG_STATE_HOME=/tmp/t/home/.local/state XDG_DATA_HOME=/tmp/t/home/.local/share DISPLAY=:97 XDG_CURRENT_DESKTOP=TEST XDG_SESSION_TYPE=x11 FRACTILE_NO_SESSION_MANAGER=1 PATH=/src/target/debug:/src/ui:/usr/bin:/bin
Xvfb :97 -screen 0 1600x1000x24 >/dev/null 2>&1 & sleep 1
kwin_x11 >/dev/null 2>&1 & sleep 2
exec dbus-run-session -- bash -euo pipefail -c "
mkdir -p \$XDG_CONFIG_HOME/fractile; printf \"[general]\nfirst_run_complete = true\n\" > \$XDG_CONFIG_HOME/fractile/config.toml
n() { pgrep -c -f \"^python3 /.*fractile-ui\" || true; }
fractiled --backend x11 >/dev/null 2>&1 & D=\$!; sleep 36
[ \"\$(n)\" = 0 ] || { echo \"FAIL: the tray-only UI did not leave without a tray\"; exit 1; }
python3 /src/ui/fractile-ui --launcher >/dev/null 2>&1 & sleep 4
kill \$D; wait \$D 2>/dev/null || true
fractiled --backend x11 >/dev/null 2>&1 & D=\$!
worst=0; for t in 1 2 3 4 5 6 7 8 9 10 11 12; do sleep 1; c=\$(n); [ \$c -gt \$worst ] && worst=\$c; done
left=\$(pgrep -a -f \"^python3 /.*fractile-ui\" | cut -d\" \" -f2- || true)
kill \$D || true
if [ \"\$(echo \"\$left\" | grep -c .)\" = 1 ] && echo \"\$left\" | grep -q -- --launcher; then
  echo \"PASS: one UI after the restart, the one opened from the menu (at most \$worst at once)\"
else
  echo \"FAIL: after the restart still running: \$left (at most \$worst at once)\"; exit 1
fi"
'
