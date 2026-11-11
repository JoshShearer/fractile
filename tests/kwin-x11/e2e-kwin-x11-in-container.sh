#!/usr/bin/env bash
# Runs the X11 E2E suite (tests/integration/e2e_x11.py) with kwin_x11 as the
# window manager, in an Ubuntu 24.04 container, as an unprivileged user.
# Uses the host's debug build (Ubuntu 24.04 and the host share glibc 2.39);
# run `cargo build` first. Evidence: tests/evidence/e2e-x11-kwin.json.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
docker image inspect fractile-kwin-x11-test >/dev/null 2>&1 || docker build -t fractile-kwin-x11-test "$root/tests/kwin-x11"
docker run --rm -v "$root:/src:ro" -v "$root/tests/evidence:/evidence" fractile-kwin-x11-test bash -c '
  chown tester /evidence
  exec su tester -s /bin/bash -c "
    export XDG_RUNTIME_DIR=/tmp/rt PYTHONDONTWRITEBYTECODE=1 FRACTILE_TEST_WM=kwin FRACTILE_TEST_XSERVER=xvfb FRACTILE_BIN_DIR=/src/target/debug
    mkdir -m700 -p \$XDG_RUNTIME_DIR
    kwin_x11 --version
    python3 /src/tests/integration/e2e_x11.py --display 40 --evidence /evidence/e2e-x11-kwin.json"'
