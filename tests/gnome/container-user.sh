#!/usr/bin/env bash
# Runs as the unprivileged test user inside the GNOME test container.
set -uo pipefail
export PATH=/toolchain/bin:$PATH CARGO_HOME=/cache/cargo CARGO_TARGET_DIR=/cache/target
cd /src && cargo build --locked -q -p fractile-daemon -p fractile-cli 2>&1 | tail -5
[ -x /cache/target/debug/fractiled ] && [ -x /cache/target/debug/fractile ] || { echo "build failed"; exit 2; }
export XDG_RUNTIME_DIR=/tmp/rt; mkdir -m700 -p "$XDG_RUNTIME_DIR"
export FRACTILE_BIN_DIR=/cache/target/debug
export FRACTILE_APPINDICATOR_DIR=/usr/share/gnome-shell/extensions/appindicatorsupport@rgcjonas.gmail.com
# The source tree is read-only: screenshots go to a scratch dir.
export PYTHONDONTWRITEBYTECODE=1
export FRACTILE_SHOT_DIR=/tmp/shots; mkdir -p "$FRACTILE_SHOT_DIR"
major=$(gnome-shell --version | grep -o '[0-9]*' | head -1)
echo "== $(gnome-shell --version), $(. /etc/os-release; echo "$PRETTY_NAME")"
rc=0
for s in "$@"; do
  n=$(basename "$s" .py | tr _ -)
  echo "== $n"
  python3 "/src/$s" --evidence "/evidence/$n-gnome$major.json" || rc=1
done
exit $rc
