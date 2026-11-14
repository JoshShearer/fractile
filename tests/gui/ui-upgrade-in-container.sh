#!/usr/bin/env bash
# Regression test: a running UI restarts onto new code after a package
# upgrade. Seen on Pop!_OS and Omarchy: after reinstalling, the UI started
# before the upgrade kept drawing the old panel until it was killed.
#  A: files change while the UI is idle -> it restarts within two checks.
#  B: files change, then the panel shortcut -> it restarts and shows the new
#     panel (asked for again with --panel).
# Needs the debug build (cargo build) and the kwin-x11 test image.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
docker run --rm --user "$(id -u):$(id -g)" -v "$root:/src:ro" fractile-kwin-x11-test:latest bash -euo pipefail -c '
mkdir -p /tmp/t/home /tmp/r; chmod 700 /tmp/r
export HOME=/tmp/t/home XDG_RUNTIME_DIR=/tmp/r XDG_CONFIG_HOME=/tmp/t/home/.config XDG_STATE_HOME=/tmp/t/home/.local/state XDG_DATA_HOME=/tmp/t/home/.local/share DISPLAY=:97 XDG_CURRENT_DESKTOP=TEST XDG_SESSION_TYPE=x11 FRACTILE_NO_SESSION_MANAGER=1 PATH=/src/target/debug:/usr/bin:/bin
cp -r /src/ui /tmp/ui; rm -rf /tmp/ui/fractile_ui/__pycache__
Xvfb :97 -screen 0 1600x1000x24 >/dev/null 2>&1 & sleep 1
kwin_x11 >/dev/null 2>&1 & sleep 2
exec dbus-run-session -- bash -euo pipefail -c "
mkdir -p \$XDG_CONFIG_HOME/fractile; printf \"[general]\nfirst_run_complete = true\ntray_icon = false\n\" > \$XDG_CONFIG_HOME/fractile/config.toml
export FRACTILE_UI=/tmp/ui/fractile-ui FRACTILE_UI_TEST_GEOMETRY=/tmp/geom.json
fractiled --backend x11 >/tmp/d.log 2>&1 & D=\$!; sleep 2
python3 /tmp/ui/fractile-ui >/tmp/ui.log 2>&1 & U=\$!; sleep 4
mark() { echo \"import sys as _s; print(\\\"NEWCODE-\$1\\\", _s.argv[1:], file=_s.stderr)\" >> /tmp/ui/fractile_ui/__init__.py; }
fail() { echo \"FAIL: \$*\"; tail -20 /tmp/ui.log; kill \$D; exit 1; }
mark A; sleep 50
grep -q \"NEWCODE-A \[\]\" /tmp/ui.log || fail \"A: the idle UI did not restart onto the new code\"
kill -0 \$U || fail \"A: the UI process is gone\"
mark B; sleep 1
python3 -c \"import sys; sys.path.insert(0, \\\"/src/ui\\\"); from fractile_ui.ipc import Client; Client().call(\\\"show_panel\\\", {})\"
sleep 5
grep -q \"NEWCODE-B \[.--panel.\]\" /tmp/ui.log || fail \"B: the shortcut did not restart the UI onto the new code\"
[ -s /tmp/geom.json ] || fail \"B: no panel after the restart\"
n=\$(grep -c \"restarting\" /tmp/ui.log)
[ \"\$n\" = 2 ] || fail \"expected 2 restarts, saw \$n\"
kill \$D || true
echo \"PASS: restarted onto new code when idle and at the panel shortcut; the new panel showed\"
"
'
