#!/usr/bin/env bash
# Test container only: register the host's NVIDIA EGL/GBM userspace
# (mounted read-only at /hostlib, /hostegl) so kwin_wayland can use the
# passed-through render node.
set -euo pipefail
for f in /hostlib/libEGL_nvidia.so.* /hostlib/libGLESv2_nvidia.so.* /hostlib/libnvidia-*.so.*; do cp -P "$f" /usr/lib/; done
mkdir -p /usr/lib/gbm /usr/share/egl/egl_external_platform.d /usr/share/glvnd/egl_vendor.d
ln -sf /usr/lib/libnvidia-allocator.so.1 /usr/lib/gbm/nvidia-drm_gbm.so
cp /hostegl/vendor/*nvidia*.json /usr/share/glvnd/egl_vendor.d/
[ -d /hostegl/platform ] && cp /hostegl/platform/*.json /usr/share/egl/egl_external_platform.d/
ldconfig
