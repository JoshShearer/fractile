#!/usr/bin/env bash
# Runs the two-monitor GUI check (tests/gui/gui_two_monitors.py: the real PyQt6 UI, real
# X mouse events, two RandR monitors) with kwin_x11 as the window manager, on Xvfb, in the
# kwin-x11 test image, as an unprivileged user. Nothing appears on the host
# screen. Uses the host's debug build; run `cargo build` first.
# Evidence: tests/evidence/gui-two-monitors-kwin.json.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
docker image inspect fractile-kwin-x11-test >/dev/null 2>&1 || docker build -t fractile-kwin-x11-test "$root/tests/kwin-x11"
docker run --rm -v "$root:/src:ro" -v "$root/tests/evidence:/evidence" fractile-kwin-x11-test bash -c '
  chown tester /evidence
  exec su tester -s /bin/bash -c "
    export XDG_RUNTIME_DIR=/tmp/rt PYTHONDONTWRITEBYTECODE=1 FRACTILE_TEST_WM=kwin FRACTILE_TEST_XSERVER=xvfb FRACTILE_BIN_DIR=/src/target/debug FRACTILE_GUI_SHOTS=/tmp/shots
    mkdir -m700 -p \$XDG_RUNTIME_DIR
    kwin_x11 --version
    python3 /src/tests/gui/gui_two_monitors.py --display 43 --evidence /evidence/gui-two-monitors-kwin.json"' "$@"
