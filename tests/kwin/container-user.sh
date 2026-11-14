#!/usr/bin/env bash
# Runs as the unprivileged test user inside the container.
set -euo pipefail
export CARGO_HOME=/cache/cargo CARGO_TARGET_DIR=/cache/target
cd /src && cargo build --locked -q -p fractile-daemon -p fractile-cli 2>&1 | tail -5
export XDG_RUNTIME_DIR=/tmp/rt; mkdir -m700 -p "$XDG_RUNTIME_DIR"
# Test harness only: KWin offers its fake-input protocol to clients whose
# desktop file asks for it; tests/tools/kwin_fake_input.py runs under this
# interpreter (the two-monitor suite drags with it).
mkdir -p ~/.local/share/applications
printf '[Desktop Entry]\nType=Application\nName=Fractile test pointer\nExec=%s\nNoDisplay=true\nX-KDE-Wayland-Interfaces=org_kde_kwin_fake_input\n' "$(readlink -f /usr/bin/python3)" \
  > ~/.local/share/applications/fractile-test-pointer.desktop
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
exec dbus-run-session -- bash -c '
QT_FORCE_STDERR_LOGGING=1 QT_LOGGING_RULES="kwin_scripting.debug=true;js.debug=true;qml.debug=true" \
  kwin_wayland --virtual --no-lockscreen --width 1600 --height 1200 --output-count "${FRACTILE_KWIN_OUTPUTS:-1}" --socket wl-0 >/tmp/kwin.log 2>&1 &
for i in $(seq 50); do [ -S "$XDG_RUNTIME_DIR/wl-0" ] && break; sleep 0.2; done
sleep 2
if grep -q "Found no render device" /tmp/kwin.log; then echo "WARNING: kwin has no render device; GTK4 resizes will not complete"; fi
export WAYLAND_DISPLAY=wl-0 FRACTILE_BIN_DIR=/cache/target/debug FRACTILE_SRC=/src EVIDENCE=/evidence/${FRACTILE_KWIN_EVIDENCE:-e2e-kwin-wayland.json}
rc=0; python3 /src/tests/kwin/${FRACTILE_KWIN_SUITE:-e2e_kwin.py} || rc=$?
[ $rc = 0 ] || { echo "== kwin log"; grep -v -E "^js: |kwin_scripting" /tmp/kwin.log | tail -40; }
exit $rc
'
