#!/usr/bin/env bash
# Runs as the unprivileged test user inside the container.
set -euo pipefail
export CARGO_HOME=/cache/cargo CARGO_TARGET_DIR=/cache/target
cd /src && cargo build --locked -q -p fractile-daemon -p fractile-cli 2>&1 | tail -5
export XDG_RUNTIME_DIR=/tmp/rt; mkdir -m700 -p "$XDG_RUNTIME_DIR"
# A user configuration with one bind of its own (SUPER+Q), used by the
# conflict check. resolve_binds_by_sym: wtype uploads its own keymap with
# keysyms at arbitrary keycodes, so binds must match by keysym for the
# harness key presses (a physical keyboard needs no such setting).
mkdir -p ~/.config/hypr
cat > ~/.config/hypr/hyprland.lua <<'LUA'
hl.monitor({ output = "", mode = "1600x1200@60", position = "0x0", scale = 1 })
hl.config({ misc = { disable_hyprland_logo = true, disable_splash_rendering = true }, input = { resolve_binds_by_sym = true } })
hl.bind("SUPER + Q", hl.dsp.window.close())
LUA
exec dbus-run-session -- bash -c '
kwin_wayland --virtual --no-lockscreen --width 1600 --height 1200 --socket wl-0 >/tmp/kwin.log 2>&1 &
for i in $(seq 50); do [ -S "$XDG_RUNTIME_DIR/wl-0" ] && break; sleep 0.2; done
sleep 2
export FRACTILE_BIN_DIR=/cache/target/debug FRACTILE_SRC=/src EVIDENCE=/evidence/e2e-hyprland.json HYPRLAND_LOG=/tmp/hyprland.log
rc=0; python3 /src/tests/hyprland/e2e_hyprland.py || rc=$?
[ $rc = 0 ] || { echo "== hyprland log"; tail -30 /tmp/hyprland.log; }
exit $rc
'
