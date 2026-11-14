#!/usr/bin/env bash
# Regression test: the service restarts onto a new binary after a package
# upgrade (seen on Pop!_OS and Omarchy: the old fractiled kept running until
# the next login). The binary is replaced the way dpkg and pacman do it
# (write a new file, rename it over the old one).
#  1. While the panel is open it does not restart.
#  2. Once the panel is closed it restarts in place (same pid, new binary),
#     the UI reconnects, and the panel works again.
# Needs the debug build (cargo build) and the kwin-x11 test image.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
docker run --rm --user "$(id -u):$(id -g)" -v "$root:/src:ro" fractile-kwin-x11-test:latest bash -euo pipefail -c '
mkdir -p /tmp/t/home /tmp/r /tmp/bin; chmod 700 /tmp/r
cp /src/target/debug/fractiled /tmp/bin/fractiled
export HOME=/tmp/t/home XDG_RUNTIME_DIR=/tmp/r XDG_CONFIG_HOME=/tmp/t/home/.config XDG_STATE_HOME=/tmp/t/home/.local/state XDG_DATA_HOME=/tmp/t/home/.local/share DISPLAY=:97 XDG_CURRENT_DESKTOP=TEST XDG_SESSION_TYPE=x11 FRACTILE_NO_SESSION_MANAGER=1 PATH=/tmp/bin:/src/target/debug:/usr/bin:/bin
Xvfb :97 -screen 0 1600x1000x24 >/dev/null 2>&1 & sleep 1
kwin_x11 >/dev/null 2>&1 & sleep 2
exec dbus-run-session -- bash -euo pipefail -c "
mkdir -p \$XDG_CONFIG_HOME/fractile; printf \"[general]\nfirst_run_complete = true\ntray_icon = false\n\" > \$XDG_CONFIG_HOME/fractile/config.toml
export FRACTILE_UI=/src/ui/fractile-ui
/tmp/bin/fractiled --backend x11 >/tmp/d.out 2>&1 & D=\$!; sleep 2
python3 /src/ui/fractile-ui >/tmp/ui.log 2>&1 & sleep 4
call() { python3 -c \"import sys, json; sys.path.insert(0, \\\"/src/ui\\\"); from fractile_ui.ipc import Client; print(json.dumps(Client().call(\\\"\$1\\\", {})))\"; }
log=\$(find \$XDG_STATE_HOME -name \"*.log\" | head -1)
fail() { echo \"FAIL: \$*\"; tail -15 \$log; exit 1; }
call show_panel >/dev/null; sleep 1
cp /src/target/debug/fractiled /tmp/bin/.fractiled.new && mv -f /tmp/bin/.fractiled.new /tmp/bin/fractiled
readlink /proc/\$D/exe | grep -q deleted || fail \"the replaced binary does not show as deleted\"
sleep 110
grep -q \"restarting onto\" \$log && fail \"restarted while the panel was open\"
grep -q \"the installed service changed\" \$log || fail \"the new binary was not noticed\"
call toggle_panel >/dev/null
for i in \$(seq 1 30); do grep -q \"restarting onto\" \$log && break; sleep 2; done
grep -q \"restarting onto\" \$log || fail \"no restart after the panel closed\"
sleep 6
kill -0 \$D || fail \"the service process is gone\"
readlink /proc/\$D/exe | grep -q deleted && fail \"still running the old binary\"
[ \$(grep -c \"starting pid=\$D\" \$log) = 2 ] || fail \"expected two starts with pid \$D\"
uis=\$(call status | python3 -c \"import sys, json; print(len(json.load(sys.stdin)[\\\"uis\\\"]))\")
[ \"\$uis\" = 1 ] || fail \"expected the UI to reconnect, attached UIs: \$uis\"
[ \"\$(call show_panel | python3 -c \"import sys, json; print(json.load(sys.stdin)[\\\"visible\\\"])\")\" = True ] || fail \"the panel did not open after the restart\"
kill \$D || true
echo \"PASS: no restart while the panel was open; restarted in place (pid \$D) onto the new binary after it closed; the UI reconnected and the panel opened\"
"
'
