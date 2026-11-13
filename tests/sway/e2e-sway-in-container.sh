#!/usr/bin/env bash
# Sway E2E: builds Fractile inside the Sway test image
# (tests/sway/Dockerfile, built on the KWin image), starts kwin_wayland
# --virtual as the parent display, runs Sway nested in it, and runs
# tests/sway/e2e_sway.py. Writes evidence to
# tests/evidence/e2e-sway.json.
#   tests/sway/e2e-sway-in-container.sh
#
# Sway uses wlroots' Wayland backend here (a window of a headless KWin) with
# the pixman renderer; KWin uses the host render node when there is one (see
# tests/kwin/e2e-kwin-in-container.sh for the GPU setup).
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
docker image inspect fractile-kwin-test >/dev/null 2>&1 || docker build -t fractile-kwin-test "$root/tests/kwin"
docker build -q -t fractile-sway-test "$root/tests/sway" >/dev/null
mkdir -p "$root/tests/evidence"
args=(--rm --cap-add SYS_NICE -v "$root:/src:ro" -v fractile-kwin-cache:/cache -v "$root/tests/evidence:/evidence")
gpu=none
if [ -e /dev/dri/renderD128 ]; then
  args+=(--device /dev/dri/renderD128)
  gpu=mesa
  libdir=/usr/lib/x86_64-linux-gnu; [ -e $libdir/libEGL_nvidia.so.0 ] || libdir=/usr/lib
  if [ -e $libdir/libEGL_nvidia.so.0 ] && [ -e /dev/nvidiactl ]; then
    gpu=nvidia
    for d in /dev/nvidiactl /dev/nvidia0 /dev/nvidia-modeset; do [ -e $d ] && args+=(--device $d); done
    args+=(-v "$libdir:/hostlib:ro" -v /usr/share/glvnd/egl_vendor.d:/hostegl/vendor:ro)
    [ -d /usr/share/egl/egl_external_platform.d ] && args+=(-v /usr/share/egl/egl_external_platform.d:/hostegl/platform:ro)
  fi
fi
echo "render device: $gpu"
docker run "${args[@]}" -e FRACTILE_TEST_GPU=$gpu fractile-sway-test bash /src/tests/sway/container-root.sh
