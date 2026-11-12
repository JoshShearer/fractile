#!/usr/bin/env bash
# Start an isolated nested X11 desktop: Xephyr + real GNOME Shell (mutter) WM
# with a private D-Bus session. Prints "DISPLAY=:N XEPHYR_PID=.. SHELL_PID=..".
# Usage: nested-gnome-x11.sh start <display-num> [WxH[+WxH]] | stop <display-num>
# FRACTILE_TEST_WM=kwin runs kwin_x11 (Plasma X11) instead of gnome-shell, and
# FRACTILE_TEST_XSERVER=xvfb uses Xvfb instead of Xephyr (containers).
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
  # Stop the watchdog unless this stop is the watchdog's own.
  if [[ -f $state/watchdog.pid ]]; then wpid=$(cat "$state/watchdog.pid"); [[ $wpid != "$$" ]] && kill "$wpid" 2>/dev/null || true; fi
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
# Watchdog: when the process that started this desktop exits without running
# "stop" (killed, timed out, the terminal closed), stop it anyway, so a test
# shell and its bus never outlive their test. The owner is the caller
# (FRACTILE_TEST_OWNER_PID overrides it).
owner=${FRACTILE_TEST_OWNER_PID:-$PPID}
if (( owner > 1 )); then
  setsid bash -c 'while kill -0 "$1" 2>/dev/null && [[ -d $3 ]]; do sleep 2; done; [[ -d $3 ]] && exec bash "$2" stop "$4"' \
    _ "$owner" "$(realpath "$0")" "$state" "$num" </dev/null >/dev/null 2>&1 &
  echo $! >"$state/watchdog.pid"
fi
IFS=+ read -ra scr <<<"$screens"
for s in "${scr[@]}"; do args+=(-screen "$s"); done
# multiple -screen with +xinerama gives a multi-monitor RandR layout
if (( ${#scr[@]} > 1 )); then args+=(+xinerama); fi
if [[ ${FRACTILE_TEST_XSERVER:-xephyr} == xvfb ]]; then
  # Xvfb: one screen (multi-monitor runs use Xephyr)
  Xvfb ":$num" -screen 0 "${scr[0]}x24" -ac -noreset +extension RANDR >"$state/xephyr.log" 2>&1 &
else
  Xephyr ":$num" "${args[@]}" -ac -br -noreset -resizeable >"$state/xephyr.log" 2>&1 &
fi
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
wm=(gnome-shell --x11 --replace)
[[ ${FRACTILE_TEST_WM:-gnome} == kwin ]] && wm=(kwin_x11 --replace)
DISPLAY=":$num" XDG_RUNTIME_DIR="$state/rt" XDG_SESSION_TYPE=x11 GSETTINGS_BACKEND=memory DBUS_SESSION_BUS_ADDRESS="$(sed -n 1p "$state/dbus.out")" \
  "${wm[@]}" >"$state/shell.log" 2>&1 &
echo $! >"$state/shell.pid"
ok=0
for _ in $(seq 150); do DISPLAY=":$num" xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -q 'window id' && { ok=1; break; }; sleep 0.1; done
if (( ! ok )); then echo "${wm[0]} did not become the window manager; see $state/shell.log" >&2; exit 1; fi
# The WM check window appears before the shell has reserved its top bar (and
# dock, if any). Tests that read the work area too early see the whole screen,
# then mutter constrains windows a moment later. Wait for the struts.
root_wh=$(DISPLAY=":$num" xwininfo -root | awk '/Width:/{w=$2} /Height:/{h=$2} END{print w", "h}')
struts=0
tries=150; [[ ${FRACTILE_TEST_WM:-gnome} == kwin ]] && tries=10  # no panel without plasmashell
for _ in $(seq $tries); do
  wa=$(DISPLAY=":$num" xprop -root _NET_WORKAREA 2>/dev/null | sed -n 's/.*= //p' | cut -d, -f1-4)
  if [[ -n $wa && $wa != "0, 0, $root_wh" ]]; then struts=1; break; fi
  sleep 0.1
done
if (( ! struts )); then echo "warning: work area still covers the whole screen (no panel struts yet)" >&2; fi
# GNOME Shell 40 and later starts in the Overview, which holds the keyboard:
# no passive key grab (Fractile's global shortcuts included) fires until it
# closes. A user closes it at once; tests do the same, through the shell's own
# OverviewActive property.
if [[ ${FRACTILE_TEST_WM:-gnome} == gnome ]]; then
  bus=$(sed -n 1p "$state/dbus.out")
  ov() { DBUS_SESSION_BUS_ADDRESS=$bus gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell \
           --method org.freedesktop.DBus.Properties."$@" 2>/dev/null; }
  for _ in $(seq 50); do
    ov Get org.gnome.Shell OverviewActive | grep -q false && break
    ov Set org.gnome.Shell OverviewActive "<false>" >/dev/null || true
    sleep 0.2
  done
  ov Get org.gnome.Shell OverviewActive | grep -q false || echo "warning: the GNOME Shell Overview is still open; global shortcuts will not fire" >&2
fi
sleep 1
echo "DISPLAY=:$num XEPHYR_PID=$(cat "$state/xephyr.pid") SHELL_PID=$(cat "$state/shell.pid") STATE=$state DBUS_SESSION_BUS_ADDRESS=$(sed -n 1p "$state/dbus.out")"
