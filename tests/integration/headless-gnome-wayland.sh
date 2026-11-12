#!/usr/bin/env bash
# Start an isolated headless GNOME Shell (Wayland, no Xwayland) with a
# virtual monitor and a private D-Bus session, optionally with extensions
# from a private XDG_DATA_HOME enabled. Nothing touches the user's session:
# the shell gets its own XDG_RUNTIME_DIR (gnome-shell writes a crash guard
# file there that would otherwise disable the real session's extensions).
#
#   headless-gnome-wayland.sh start <name> [WxH[,WxH...]] [extension-uuid...]
# Several sizes give several virtual monitors, left to right.
#   headless-gnome-wayland.sh stop <name>
# Prints: XDG_RUNTIME_DIR=<dir> WAYLAND_DISPLAY=<name> DBUS_SESSION_BUS_ADDRESS=... STATE=<dir>
# Clients of the nested session must use that XDG_RUNTIME_DIR.
set -euo pipefail
cmd=${1:?start|stop}; name=${2:?name}; size=${3:-1600x1200}
state="${XDG_RUNTIME_DIR:?}/fractile-headless-$name"
if [[ $cmd == stop ]]; then
  # Stop the watchdog unless this stop is the watchdog's own.
  if [[ -f $state/watchdog.pid ]]; then wpid=$(cat "$state/watchdog.pid"); [[ $wpid != "$$" ]] && kill "$wpid" 2>/dev/null || true; fi
  [[ -f $state/dbus.pid ]] && kill "$(cat "$state/dbus.pid")" 2>/dev/null || true
  if [[ -f $state/shell.pid ]]; then
    spid=$(cat "$state/shell.pid"); kill "$spid" 2>/dev/null || true
    for _ in $(seq 50); do [[ -d /proc/$spid ]] || break; sleep 0.1; done
  fi
  rm -rf "$state"; exit 0
fi
[[ -e $state ]] && { echo "$state already exists; stop it first" >&2; exit 1; }
shift 3 || shift $#
mkdir -p "$state"/{config,data,cache,rt}; chmod 700 "$state/rt"
# Watchdog: when the process that started this desktop exits without running
# "stop" (killed, timed out, the terminal closed), stop it anyway, so a test
# shell and its bus never outlive their test. The owner is the caller
# (FRACTILE_TEST_OWNER_PID overrides it).
owner=${FRACTILE_TEST_OWNER_PID:-$PPID}
if (( owner > 1 )); then
  setsid bash -c 'while kill -0 "$1" 2>/dev/null && [[ -d $3 ]]; do sleep 2; done; [[ -d $3 ]] && exec bash "$2" stop "$4"' \
    _ "$owner" "$(realpath "$0")" "$state" "$name" </dev/null >/dev/null 2>&1 &
  echo $! >"$state/watchdog.pid"
fi
export XDG_CONFIG_HOME="$state/config" XDG_DATA_HOME="${FRACTILE_TEST_DATA_HOME:-$state/data}" XDG_CACHE_HOME="$state/cache"
mkdir -p "$XDG_CONFIG_HOME/glib-2.0/settings"
exts=$(printf "'%s'," "$@"); exts="[${exts%,}]"; [[ $# -eq 0 ]] && exts="@as []"
cat >"$XDG_CONFIG_HOME/glib-2.0/settings/keyfile" <<KEY
[org/gnome/shell]
enabled-extensions=$exts
disable-user-extensions=false
disable-extension-version-validation=true
welcome-dialog-last-shown-version='999'

[org/gnome/desktop/interface]
enable-animations=false
KEY
# Private session bus that outlives this script; the shell joins it. No
# service directories: nothing from the host gets D-Bus-activated into the
# test session (and clients fail fast instead of waiting on activation).
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
dbus-daemon --config-file="$state/bus.conf" --fork --print-address=1 --print-pid=1 --nopidfile >"$state/dbus.out"
addr=$(sed -n 1p "$state/dbus.out"); sed -n 2p "$state/dbus.out" >"$state/dbus.pid"
export DBUS_SESSION_BUS_ADDRESS=$addr
unset DISPLAY WAYLAND_DISPLAY
export XDG_RUNTIME_DIR="$state/rt"
mons=(); IFS=, read -ra sizes <<<"$size"; for s in "${sizes[@]}"; do mons+=(--virtual-monitor "$s"); done
GSETTINGS_BACKEND=keyfile XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=GNOME \
  gnome-shell --headless --wayland --no-x11 "${mons[@]}" --wayland-display "$name" \
  >"$state/shell.log" 2>&1 &
echo $! >"$state/shell.pid"
for _ in $(seq 150); do [[ -S $XDG_RUNTIME_DIR/$name ]] && break; sleep 0.1; done
[[ -S $XDG_RUNTIME_DIR/$name ]] || { echo "gnome-shell did not create $name; see $state/shell.log" >&2; exit 1; }
# Wait until the shell owns its bus name (startup finished).
for _ in $(seq 150); do
  gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.NameHasOwner org.gnome.Shell 2>/dev/null | grep -q true && break
  sleep 0.1
done
sleep 1
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR WAYLAND_DISPLAY=$name DBUS_SESSION_BUS_ADDRESS=$addr STATE=$state"
