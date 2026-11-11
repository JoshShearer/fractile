#!/usr/bin/env bash
# Start an isolated nested X11 desktop: Xephyr + real GNOME Shell (mutter) WM
# with a private D-Bus session. Prints "DISPLAY=:N XEPHYR_PID=.. SHELL_PID=..".
# Usage: nested-gnome-x11.sh start <display-num> [WxH[+WxH]] | stop <display-num>
set -euo pipefail
cmd=${1:-start}; num=${2:-17}; screens=${3:-1600x900}
state="${TMPDIR:-/tmp}/fractile-nested-$num"
if [[ $cmd == stop ]]; then
  for f in shell xephyr; do
    [[ -f $state/$f.pid ]] && kill "$(cat "$state/$f.pid")" 2>/dev/null || true
  done
  rm -rf "$state"; exit 0
fi
mkdir -p "$state"
args=()
IFS=+ read -ra scr <<<"$screens"
for s in "${scr[@]}"; do args+=(-screen "$s"); done
# multiple -screen with +xinerama gives a multi-monitor RandR layout
if (( ${#scr[@]} > 1 )); then args+=(+xinerama); fi
Xephyr ":$num" "${args[@]}" -ac -br -noreset -resizeable >"$state/xephyr.log" 2>&1 &
echo $! >"$state/xephyr.pid"
for _ in $(seq 50); do DISPLAY=":$num" xprop -root >/dev/null 2>&1 && break; sleep 0.1; done
export XDG_CONFIG_HOME="$state/config" XDG_DATA_HOME="$state/data" XDG_CACHE_HOME="$state/cache"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"
DISPLAY=":$num" XDG_SESSION_TYPE=x11 GSETTINGS_BACKEND=memory dbus-run-session -- gnome-shell --x11 --replace >"$state/shell.log" 2>&1 &
echo $! >"$state/shell.pid"
for _ in $(seq 100); do DISPLAY=":$num" xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -q 'window id' && break; sleep 0.1; done
sleep 1
echo "DISPLAY=:$num XEPHYR_PID=$(cat "$state/xephyr.pid") SHELL_PID=$(cat "$state/shell.pid") STATE=$state"
