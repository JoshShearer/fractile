#!/usr/bin/env bash
# Build a binary .deb for Ubuntu 24.04+ / Debian 12+ / Pop!_OS.
#   packaging/debian/build-deb.sh [--no-build]
# Output: packaging/build/fractile_<version>-1_<arch>.deb
# Set DEB_MAINTAINER="Name <email>" to put a real maintainer in the control file.
# Honours CARGO_TARGET_DIR (default: target).
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
version=$(sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -1)
arch=$(dpkg --print-architecture)
maint=${DEB_MAINTAINER:-"Fractile maintainers <maintainers@fractile.invalid>"}
if [[ ${1:-} != --no-build ]]; then
  cargo build --release --locked
fi
out="$root/packaging/build"
stage="$out/deb/fractile_${version}-1_${arch}"
rm -rf "$stage"; mkdir -p "$stage/DEBIAN"
packaging/install.sh "$stage" /usr "${CARGO_TARGET_DIR:-target}/release" >/dev/null
strip --strip-unneeded "$stage/usr/bin/fractiled" "$stage/usr/bin/fractile" 2>/dev/null || true
# Debian keeps the licence in the copyright file, not /usr/share/licenses.
rm -rf "$stage/usr/share/licenses"
install -Dm644 /dev/stdin "$stage/usr/share/doc/fractile/copyright" <<COPY
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: fractile
Source: (local build)

Files: *
Copyright: Fractile contributors
License: GPL-3+
 On Debian systems the full text of the GNU General Public License version 3
 can be found in /usr/share/common-licenses/GPL-3.
COPY
size=$(du -sk --exclude=DEBIAN "$stage" | cut -f1)
cat >"$stage/DEBIAN/control" <<CTRL
Package: fractile
Version: ${version}-1
Architecture: ${arch}
Maintainer: ${maint}
Installed-Size: ${size}
Depends: libc6 (>= 2.35), libgcc-s1, python3 (>= 3.10), python3-pyqt6
Recommends: libnotify-bin, systemd
Suggests: gnome-shell (>= 45), kwin-wayland
Section: x11
Priority: optional
Description: grid window placement and workspace session restore
 Fractile opens a small grid over the focused window; drag across cells or
 use the keyboard and the window fills that area. Global shortcuts, margins,
 gaps, per-monitor panels and named layouts are included.
 .
 It also saves which apps and windows were open and restores monitor,
 workspace, size and position after a restart or crash. It does not restore
 unsaved content inside applications, and only reopens apps you approve.
 .
 Window control: X11; GNOME Shell Wayland through the bundled GNOME Shell
 extension; KDE Plasma (Wayland and X11) through the bundled KWin script;
 Sway and Hyprland through their IPC sockets. Other Wayland compositors are
 not supported.
CTRL
echo /etc/sway/config.d/60-fractile.conf > "$stage/DEBIAN/conffiles"
# Login start is per user and opt-in; tell admins how, never enable it here.
cat >"$stage/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
if [ "$1" = configure ] && [ -n "$2" ]; then
  echo "fractile: upgraded. A running Fractile service switches to the new version by itself"
  echo "          within a couple of minutes, once it is idle. Versions before 0.8.5 do not:"
  echo "          log out and in, or run: systemctl --user restart fractile.service"
fi
exit 0
POST
chmod 755 "$stage/DEBIAN/postinst"
find "$stage" -type d -exec chmod 755 {} +
deb="$out/fractile_${version}-1_${arch}.deb"
dpkg-deb --root-owner-group -Zxz --build "$stage" "$deb" >/dev/null
echo "$deb"
