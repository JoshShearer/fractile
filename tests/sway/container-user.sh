#!/usr/bin/env bash
# Runs as the unprivileged test user inside the container.
set -euo pipefail
export CARGO_HOME=/cache/cargo CARGO_TARGET_DIR=/cache/target
cd /src && cargo build --locked -q -p fractile-daemon -p fractile-cli 2>&1 | tail -5
export XDG_RUNTIME_DIR=/tmp/rt; mkdir -m700 -p "$XDG_RUNTIME_DIR"
# A user configuration with bindings of its own, used by the conflict
# checks: Mod4+q (kill) in the main file, and Mod4+Shift+t in a file the
# main file includes.
mkdir -p ~/.config/sway/config.d
cat > ~/.config/sway/config <<'CFG'
set $mod Mod4
output * mode 1600x1200 position 0 0
default_border pixel 2
bindsym $mod+q kill
include ~/.config/sway/config.d/*
CFG
cat > ~/.config/sway/config.d/50-user <<'CFG'
bindsym $mod+Shift+t exec touch /tmp/fractile-sway-user-bind
CFG
exec dbus-run-session -- bash -c '
kwin_wayland --virtual --no-lockscreen --width 1600 --height 1200 --socket wl-0 >/tmp/kwin.log 2>&1 &
for i in $(seq 50); do [ -S "$XDG_RUNTIME_DIR/wl-0" ] && break; sleep 0.2; done
sleep 2
export FRACTILE_BIN_DIR=/cache/target/debug FRACTILE_SRC=/src EVIDENCE=/evidence/e2e-sway.json SWAY_LOG=/tmp/sway.log
rc=0; python3 /src/tests/sway/e2e_sway.py || rc=$?
[ $rc = 0 ] || { echo "== sway log"; grep -iE "error|fractile" /tmp/sway.log | tail -30; }
exit $rc
'
