#!/usr/bin/env bash
# Build a binary .deb for Ubuntu 24.04+ / Debian 12+ / Pop!_OS.
#   packaging/debian/build-deb.sh [--no-build]
# Output: packaging/build/fractile_<version>-1_<arch>.deb
# Set DEB_MAINTAINER="Name <email>" to put a real maintainer in the control file.
# Honours CARGO_TARGET_DIR (default: target).
# Also builds fractile-dbgsym_<version>-1_<arch>.deb with the binaries' debug
# info (/usr/lib/debug/.build-id/..., found by gdb and debuginfod clients);
# FRACTILE_DBGSYM=0 skips it.
# FRACTILE_TARGET=aarch64-unknown-linux-gnu cross-builds an arm64 package
# (needs that Rust target and aarch64-linux-gnu-gcc; see
# tests/cross/aarch64-in-container.sh).
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
version=$(sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -1)
target=${FRACTILE_TARGET:-}
case "$target" in
  "") arch=$(dpkg --print-architecture); bindir="${CARGO_TARGET_DIR:-target}/release"; strip=strip ;;
  aarch64-unknown-linux-gnu) arch=arm64; bindir="${CARGO_TARGET_DIR:-target}/$target/release"; strip=aarch64-linux-gnu-strip ;;
  *) echo "unsupported FRACTILE_TARGET $target" >&2; exit 2 ;;
esac
dbgsym=${FRACTILE_DBGSYM:-1}
objcopy=objcopy
[[ $target == aarch64-unknown-linux-gnu ]] && objcopy=aarch64-linux-gnu-objcopy
maint=${DEB_MAINTAINER:-"Fractile maintainers <maintainers@fractile.invalid>"}
if [[ ${1:-} != --no-build ]]; then
  if [[ $dbgsym == 1 ]]; then
    # keep debug info in the build; it is split off into the dbgsym package below
    CARGO_PROFILE_RELEASE_STRIP=none CARGO_PROFILE_RELEASE_DEBUG=limited cargo build --release --locked ${target:+--target "$target"}
  else
    cargo build --release --locked ${target:+--target "$target"}
  fi
fi
out="$root/packaging/build"
stage="$out/deb/fractile_${version}-1_${arch}"
rm -rf "$stage"; mkdir -p "$stage/DEBIAN"
packaging/install.sh "$stage" /usr "$bindir" >/dev/null
dbgstage="$out/deb/fractile-dbgsym_${version}-1_${arch}"
rm -rf "$dbgstage"
if [[ $dbgsym == 1 ]]; then
  for b in fractiled fractile; do
    f="$stage/usr/bin/$b"
    if ! readelf -S "$f" | grep -q '\.debug_info'; then
      echo "build-deb: $b has no debug info (built without it?); no dbgsym package" >&2; dbgsym=0; break
    fi
    id=$(readelf -n "$f" | sed -n 's/.*Build ID: \([0-9a-f]*\).*/\1/p')
    dbg="$dbgstage/usr/lib/debug/.build-id/${id:0:2}/${id:2}.debug"
    mkdir -p "$(dirname "$dbg")"
    "$objcopy" --only-keep-debug --compress-debug-sections "$f" "$dbg"
    chmod 644 "$dbg"
    "$strip" --strip-unneeded "$f"
    "$objcopy" --add-gnu-debuglink="$dbg" "$f"
  done
fi
"$strip" --strip-unneeded "$stage/usr/bin/fractiled" "$stage/usr/bin/fractile" 2>/dev/null || true
# Debian keeps the licence in the copyright file, not /usr/share/licenses.
rm -rf "$stage/usr/share/licenses"
install -Dm644 /dev/stdin "$stage/usr/share/doc/fractile/copyright" <<COPY
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: fractile
Source: https://github.com/JoshShearer/fractile

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
if [[ $dbgsym == 1 ]]; then
  mkdir -p "$dbgstage/DEBIAN" "$dbgstage/usr/share/doc"
  ln -s fractile "$dbgstage/usr/share/doc/fractile-dbgsym"
  ids=$(cd "$dbgstage/usr/lib/debug/.build-id" && find . -name '*.debug' | sed 's|^\./||; s|/||; s|\.debug$||' | sort | tr '\n' ' ')
  cat >"$dbgstage/DEBIAN/control" <<CTRL
Package: fractile-dbgsym
Source: fractile
Version: ${version}-1
Architecture: ${arch}
Maintainer: ${maint}
Installed-Size: $(du -sk --exclude=DEBIAN "$dbgstage" | cut -f1)
Depends: fractile (= ${version}-1)
Section: debug
Priority: optional
Build-Ids: ${ids% }
Description: debug symbols for fractile
 Debug information for the fractiled service and the fractile command, for
 debuggers and crash reports. Not needed to run Fractile.
CTRL
  find "$dbgstage" -type d -exec chmod 755 {} +
  ddeb="$out/fractile-dbgsym_${version}-1_${arch}.deb"
  dpkg-deb --root-owner-group -Zxz --build "$dbgstage" "$ddeb" >/dev/null
  echo "$ddeb"
fi
