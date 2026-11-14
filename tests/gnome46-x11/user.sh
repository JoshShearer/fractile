#!/usr/bin/env bash
# Runs as the test user inside the GNOME 46 container (see run.sh).
set -uo pipefail
[ -n "${FRACTILE_TEST_APPS:-}" ] && export FRACTILE_TEST_APPS
[ -n "${FRACTILE_CHROME_NO_SANDBOX:-}" ] && export FRACTILE_CHROME_NO_SANDBOX
export XDG_RUNTIME_DIR=/tmp/rt FRACTILE_BIN_DIR=/src/target/debug FRACTILE_TEST_XSERVER=xvfb PYTHONDONTWRITEBYTECODE=1
export FRACTILE_GUI_SHOTS=/tmp/shots FRACTILE_SHOT_DIR=/tmp/shots
mkdir -m700 -p "$XDG_RUNTIME_DIR" /tmp/shots
echo "== $(gnome-shell --version) X11 on Xvfb"
rc=0
for s in "$@"; do
  n=$(basename "$s" .py | tr _ -)
  echo "== $n"
  python3 "/src/$s" --evidence "/evidence/$n-gnome46-x11.json" || rc=1
done
exit $rc
