#!/usr/bin/env bash
# Runs as root inside the test container: GPU setup, then the suite as an
# unprivileged user.
set -euo pipefail
chown tester /cache /evidence
if [ -e /dev/dri/renderD128 ]; then
  gid=$(stat -c %g /dev/dri/renderD128)
  getent group "$gid" >/dev/null || groupadd -g "$gid" hostrender
  usermod -aG "$(getent group "$gid" | cut -d: -f1)" tester
fi
[ "${FRACTILE_TEST_GPU:-}" = nvidia ] && bash /src/tests/kwin/gpu-setup.sh
exec su tester -s /bin/bash -c "FRACTILE_TEST_GPU=${FRACTILE_TEST_GPU:-none} bash /src/tests/sway/container-user.sh"
