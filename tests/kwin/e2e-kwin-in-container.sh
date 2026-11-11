#!/usr/bin/env bash
# KWin Wayland E2E: builds Fractile inside the archlinux test image
# (tests/kwin/Dockerfile), starts a private D-Bus session and a headless
# kwin_wayland --virtual, and runs tests/kwin/e2e_kwin.py. Writes evidence
# to tests/evidence/e2e-kwin-wayland.json.
#   tests/kwin/e2e-kwin-in-container.sh
#
# kwin_wayland carries cap_sys_nice as a file capability, hence --cap-add.
#
# A render node is required. KWin 6.8 has no software compositor for the
# virtual backend; without one it never composites, sends no frame
# callbacks, and GTK4 clients ack configures but never redraw at the new
# size (Qt clients still resize). The host's /dev/dri render node is passed
# in. On NVIDIA hosts the host's matching userspace driver is mounted
# read-only and registered inside the container (tests/kwin/gpu-setup.sh),
# because the proprietary libraries must match the host kernel module.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
docker image inspect fractile-kwin-test >/dev/null 2>&1 || docker build -t fractile-kwin-test "$root/tests/kwin"
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
docker run "${args[@]}" -e FRACTILE_TEST_GPU=$gpu fractile-kwin-test bash /src/tests/kwin/container-root.sh
