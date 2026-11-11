#!/usr/bin/env bash
# Start an isolated nested X11 desktop: Xephyr + real GNOME Shell (mutter) WM
# with a private D-Bus session. Prints "DISPLAY=:N XEPHYR_PID=.. SHELL_PID=..".
# Usage: nested-gnome-x11.sh start <display-num> [WxH[+WxH]] | stop <display-num>
set -euo pipefail
cmd=${1:-start}; num=${2:-17}; screens=${3:-1600x900}
# State lives in the per-user runtime dir so start/stop agree whatever TMPDIR is.
state="${XDG_RUNTIME_DIR:-/tmp}/fractile-nested-$num"
# Every process of the nested session carries XDG_RUNTIME_DIR=$state/rt;
# kill exactly those (never by name), then unmount anything left there.
session_pids() {
  local d
  for d in /proc/[0-9]*; do
    [[ -O $d ]] || continue
    { tr '\0' '\n' <"$d/environ"; } 2>/dev/null | grep -qx "XDG_RUNTIME_DIR=$state/rt" && echo "${d#/proc/}"
  done
  return 0
}
if [[ $cmd == stop ]]; then
  for f in shell dbus xephyr; do
    [[ -f $state/$f.pid ]] && kill "$(cat "$state/$f.pid")" 2>/dev/null || true
  done
  for _ in 1 2 3; do
    pids=$(session_pids); [[ -z $pids ]] && break
    kill $pids 2>/dev/null || true; sleep 0.3
  done
  if [[ -d $state/rt ]]; then
    for m in $(awk -v p="$state/rt/" 'index($2, p) == 1 {print $2}' /proc/self/mounts); do fusermount -u "$m" 2>/dev/null || true; done
  fi
  rm -rf "$state"
  sleep 0.5
  if [[ -e /tmp/.X$num-lock ]]; then
    echo "display :$num is still in use by a process this script did not start (lock pid $(tr -d ' ' </tmp/.X$num-lock))" >&2; exit 1
  fi
  exit 0
fi
if [[ -e /tmp/.X11-unix/X$num ]] || DISPLAY=":$num" xprop -root >/dev/null 2>&1; then
  echo "display :$num is already in use; stop it first or pick another number" >&2; exit 1
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
# Private runtime dir: gnome-shell writes a crash-guard file there which, in
# the real one, would make the user's own shell start with extensions off.
mkdir -p "$state/rt"; chmod 700 "$state/rt"
# Private session bus with no service directories: nothing from the host
# (portals, gvfs, evolution, keyring...) gets D-Bus-activated into the test
# session, so nothing outlives it.
cat >"$state/bus.conf" <<BUS
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir=$state</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
BUS
XDG_RUNTIME_DIR="$state/rt" dbus-daemon --config-file="$state/bus.conf" --fork --print-address=1 --print-pid=1 --nopidfile >"$state/dbus.out"
sed -n 2p "$state/dbus.out" >"$state/dbus.pid"
DISPLAY=":$num" XDG_RUNTIME_DIR="$state/rt" XDG_SESSION_TYPE=x11 GSETTINGS_BACKEND=memory DBUS_SESSION_BUS_ADDRESS="$(sed -n 1p "$state/dbus.out")" \
  gnome-shell --x11 --replace >"$state/shell.log" 2>&1 &
echo $! >"$state/shell.pid"
ok=0
for _ in $(seq 150); do DISPLAY=":$num" xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -q 'window id' && { ok=1; break; }; sleep 0.1; done
if (( ! ok )); then echo "gnome-shell did not become the window manager; see $state/shell.log" >&2; exit 1; fi
# The WM check window appears before the shell has reserved its top bar (and
# dock, if any). Tests that read the work area too early see the whole screen,
# then mutter constrains windows a moment later. Wait for the struts.
root_wh=$(DISPLAY=":$num" xwininfo -root | awk '/Width:/{w=$2} /Height:/{h=$2} END{print w", "h}')
struts=0
for _ in $(seq 150); do
  wa=$(DISPLAY=":$num" xprop -root _NET_WORKAREA 2>/dev/null | sed -n 's/.*= //p' | cut -d, -f1-4)
  if [[ -n $wa && $wa != "0, 0, $root_wh" ]]; then struts=1; break; fi
  sleep 0.1
done
if (( ! struts )); then echo "warning: work area still covers the whole screen (no panel struts yet)" >&2; fi
sleep 1
echo "DISPLAY=:$num XEPHYR_PID=$(cat "$state/xephyr.pid") SHELL_PID=$(cat "$state/shell.pid") STATE=$state DBUS_SESSION_BUS_ADDRESS=$(sed -n 1p "$state/dbus.out")"
