#!/usr/bin/env bash
# Build the Arch package with makepkg (as an unprivileged user), install it,
# exercise it and remove it, inside a clean archlinux container.
#   packaging/test/arch-in-container.sh
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
tarball=$("$root/packaging/arch/make-srctar.sh")
docker run --rm -v "$root/packaging/arch/PKGBUILD:/in/PKGBUILD:ro" -v "$tarball:/in/$(basename "$tarball"):ro" archlinux:latest bash -euo pipefail -c '
pacman -Syu --noconfirm --needed base-devel rust python-pyqt6 xorg-server-xvfb openbox xorg-xeyes >/tmp/pacman.log 2>&1 || { tail -20 /tmp/pacman.log; exit 1; }
useradd -m builder
mkdir /build && cp /in/* /build/ && chown -R builder /build
su builder -s /bin/bash -c "cd /build && makepkg --noconfirm >/tmp/makepkg.log 2>&1" || { tail -40 /tmp/makepkg.log; exit 1; }
grep -E "^test result|Finished" /tmp/makepkg.log | tail -4
pkg=$(ls /build/fractile-*.pkg.tar.zst | head -1); echo "== built $(basename "$pkg")"
pacman -U --noconfirm "$pkg" >/dev/null
echo "== integrations shipped:"; pacman -Ql fractile | grep -E "extensions/fractile@fractile.io/metadata.json|kwin/scripts/fractile/metadata.json"
pacman -Qi fractile | grep -E "^(Name|Version|Depends On)"
su builder -s /bin/bash -c "
set -euo pipefail
export XDG_RUNTIME_DIR=/tmp/rt-b; mkdir -p -m 700 \$XDG_RUNTIME_DIR
fractile --version; fractiled --version; fractile-ui --version
Xvfb :5 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1
export DISPLAY=:5 FRACTILE_NO_LOGIND=1
openbox >/dev/null 2>&1 & sleep 1
xeyes -geometry 300x200+50+50 >/dev/null 2>&1 & sleep 1
fractiled >/tmp/d.log 2>&1 & sleep 1.5
fractile status
fractile list-windows
w=\$(fractile --json list-windows | python3 -c \"import json,sys; print(json.load(sys.stdin)[0][\\\"id\\\"])\")
fractile place --grid 2x2 --cells 1,1,1,1 --window \$w
fractile save --name container-test
QT_QPA_PLATFORM=offscreen timeout 6 fractile-ui >/tmp/ui.log 2>&1 & sleep 4
grep -i -E \"Traceback|Error\" /tmp/ui.log && exit 1 || echo \"== UI started without Python errors\"
fractile quit || true
"
pacman -R --noconfirm fractile >/dev/null
for f in /usr/bin/fractiled /usr/bin/fractile /usr/bin/fractile-ui /usr/lib/systemd/user/fractile.service /usr/lib/fractile /usr/share/gnome-shell/extensions/fractile@fractile.io /usr/share/kwin/scripts/fractile; do
  [ -e "$f" ] && { echo "left behind after remove: $f"; exit 1; }
done
echo "== removed cleanly"
'
