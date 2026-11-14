#!/usr/bin/env bash
# Regression test: the panel opens when asked for just after the service
# started a tray-only UI that has already left.
# Found by the Orca suite: the service starts a UI in the background for the
# tray icon; that UI leaves at once when the icon is turned off (or yields
# to another UI, or crashes). The service would not start another UI for 10 s
# after the first, so a panel shortcut pressed in that time was parked and
# never shown.
# Needs the debug build (cargo build) and the kwin-x11 test image.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
docker run --rm --user "$(id -u):$(id -g)" -v "$root:/src:ro" fractile-kwin-x11-test:latest bash -euo pipefail -c '
mkdir -p /tmp/t/home /tmp/r; chmod 700 /tmp/r
export HOME=/tmp/t/home XDG_RUNTIME_DIR=/tmp/r XDG_CONFIG_HOME=/tmp/t/home/.config XDG_STATE_HOME=/tmp/t/home/.local/state XDG_DATA_HOME=/tmp/t/home/.local/share DISPLAY=:95 XDG_CURRENT_DESKTOP=KDE XDG_SESSION_TYPE=x11 FRACTILE_NO_SESSION_MANAGER=1 PYTHONDONTWRITEBYTECODE=1 PATH=/src/target/debug:/src/ui:/usr/bin:/bin
Xvfb :95 -screen 0 1600x1000x24 >/dev/null 2>&1 & sleep 1
kwin_x11 >/dev/null 2>&1 & sleep 2
exec dbus-run-session -- bash -euo pipefail -c "
mkdir -p \$XDG_CONFIG_HOME/fractile; printf \"[general]\nfirst_run_complete = true\n\" > \$XDG_CONFIG_HOME/fractile/config.toml
python3 /src/tests/tools/testwin.py --toolkit gtk4 --app-id org.fractile.Early --title Target >/dev/null 2>&1 &
sleep 2
fractiled --backend x11 >/tmp/t/daemon.log 2>&1 & D=\$!
for _ in \$(seq 50); do fractile status >/dev/null 2>&1 && break; sleep 0.1; done
fractile config set general.tray_icon false >/dev/null   # the tray-only UI now leaves
for _ in \$(seq 40); do pgrep -f \"^python3 /.*fractile-ui\" >/dev/null || break; sleep 0.25; done
pgrep -f \"^python3 /.*fractile-ui\" >/dev/null && { echo \"SETUP: the tray-only UI did not leave\"; kill \$D; exit 2; }
fractile panel show >/dev/null
ok=0; for _ in \$(seq 40); do xdotool search --onlyvisible --name \"Fractile panel\" >/dev/null 2>&1 && { ok=1; break; }; sleep 0.25; done
kill \$D || true
if [ \$ok = 1 ]; then echo \"PASS: the panel opened although the tray-only UI had just left\"; else
  echo \"FAIL: no panel within 10 s of asking (the tray-only UI had left just before)\"; grep -i \"ui\" /tmp/t/daemon.log | tail -5; exit 1; fi"
'
