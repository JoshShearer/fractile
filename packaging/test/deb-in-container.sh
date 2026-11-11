#!/usr/bin/env bash
# Install, exercise and remove the .deb inside a clean container.
#   packaging/test/deb-in-container.sh <image> <deb>
# Runs as root only to install packages; every Fractile command runs as an
# unprivileged user, the same way it runs on a desktop.
set -euo pipefail
image=${1:?image}; deb=$(realpath "${2:?deb}")
docker run --rm -v "$deb:/pkg/fractile.deb:ro" "$image" bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq /pkg/fractile.deb xvfb xauth >/tmp/apt.log 2>&1 || { tail -30 /tmp/apt.log; exit 1; }
echo "== installed: $(dpkg-query -W -f="\${Package} \${Version} \${Status}" fractile)"
echo "== root refused:"; fractiled --version >/dev/null; (fractiled 2>&1 || true) | head -1
useradd -m u
su u -s /bin/bash -c "
set -euo pipefail
export XDG_RUNTIME_DIR=/tmp/rt-u; mkdir -p -m 700 \$XDG_RUNTIME_DIR
fractile --version; fractiled --version; fractile-ui --version
if command -v systemd-analyze >/dev/null; then systemd-analyze --user verify /usr/lib/systemd/user/fractile.service && echo \"== unit verifies\"; fi
echo \"== headless check (no display):\"; fractiled --check > /tmp/check.json; head -c 400 /tmp/check.json; echo
echo \"== service on Xvfb (X11 without a window manager):\"
Xvfb :5 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1
export DISPLAY=:5 FRACTILE_NO_LOGIND=1
fractiled >/tmp/d.log 2>&1 & sleep 1.5
fractile status
fractile diagnose > /tmp/diag.txt; head -12 /tmp/diag.txt
fractile save --name container-test
fractile list-snapshots
QT_QPA_PLATFORM=offscreen timeout 6 fractile-ui >/tmp/ui.log 2>&1 & sleep 4
fractile status | grep -i ui || true
grep -i -E \"Traceback|Error\" /tmp/ui.log && exit 1 || echo \"== UI started without Python errors\"
fractile quit || true; sleep 1
"
apt-get remove -y -qq fractile >/dev/null 2>&1
for f in /usr/bin/fractiled /usr/bin/fractile /usr/bin/fractile-ui /usr/lib/systemd/user/fractile.service /usr/lib/fractile; do
  [ -e "$f" ] && { echo "left behind after remove: $f"; exit 1; }
done
echo "== removed cleanly"
'
