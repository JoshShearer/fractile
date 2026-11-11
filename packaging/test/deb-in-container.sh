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
apt-get install -y -qq /pkg/fractile.deb xvfb xauth openbox x11-apps >/tmp/apt.log 2>&1 || { tail -30 /tmp/apt.log; exit 1; }
echo "== installed: $(dpkg-query -W -f="\${Package} \${Version} \${Status}" fractile)"
echo "== integrations shipped, not enabled:"; ls /usr/share/gnome-shell/extensions/fractile@fractile.io/ /usr/share/kwin/scripts/fractile/contents/code/
echo "== root refused:"; fractiled --version >/dev/null; (fractiled 2>&1 || true) | head -1
useradd -m u
su u -s /bin/bash -c "
set -euo pipefail
export XDG_RUNTIME_DIR=/tmp/rt-u; mkdir -p -m 700 \$XDG_RUNTIME_DIR
fractile --version; fractiled --version; fractile-ui --version
if command -v systemd-analyze >/dev/null; then systemd-analyze --user verify /usr/lib/systemd/user/fractile.service && echo \"== unit verifies\"; fi
echo \"== headless check (no display):\"; fractiled --check > /tmp/check.json; head -c 400 /tmp/check.json; echo
echo \"== service on Xvfb with openbox (an EWMH window manager):\"
Xvfb :5 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1
export DISPLAY=:5 FRACTILE_NO_LOGIND=1
openbox >/dev/null 2>&1 & sleep 1
xeyes -geometry 300x200+50+50 >/dev/null 2>&1 & sleep 1
fractiled >/tmp/d.log 2>&1 & sleep 1.5
fractile status
fractile diagnose > /tmp/diag.txt; head -12 /tmp/diag.txt
fractile list-windows
w=\$(fractile --json list-windows | python3 -c \"import json,sys; print(json.load(sys.stdin)[0][\\\"id\\\"])\")
fractile place --grid 2x2 --cells 0,0,0,0 --window \$w
echo \"== placed: \$(fractile --json list-windows | python3 -c \"import json,sys; print(json.load(sys.stdin)[0][\\\"frame\\\"])\")\"
fractile save --name container-test
fractile list-snapshots
fractile config set general.show_help false >/dev/null
fractile autostart on
if [ -L ~/.config/systemd/user/graphical-session.target.wants/fractile.service ]; then echo \"== autostart via the systemd user unit\";
elif grep -q \"^TryExec=/usr/bin/fractiled\" ~/.config/autostart/io.fractile.Fractile.desktop; then echo \"== autostart via XDG entry with TryExec\";
else echo \"autostart on left no unit link and no XDG entry\"; exit 1; fi
QT_QPA_PLATFORM=offscreen timeout 6 fractile-ui >/tmp/ui.log 2>&1 & sleep 4
fractile status | grep -i ui || true
grep -i -E \"Traceback|Error\" /tmp/ui.log && exit 1 || echo \"== UI started without Python errors\"
fractile quit || true; sleep 1
"
apt-get remove -y -qq fractile >/dev/null 2>&1
for f in /usr/bin/fractiled /usr/bin/fractile /usr/bin/fractile-ui /usr/lib/systemd/user/fractile.service /usr/lib/fractile /usr/share/gnome-shell/extensions/fractile@fractile.io /usr/share/kwin/scripts/fractile; do
  [ -e "$f" ] && { echo "left behind after remove: $f"; exit 1; }
done
echo "== removed cleanly"
echo "== T28 lifecycle: per-user data survives remove, reinstall picks it up, purge-data removes it"
su u -s /bin/bash -c "test -s ~/.config/fractile/config.toml && test -s ~/.local/share/fractile/fractile.db && echo \"   user data kept after remove\""
apt-get install -y -qq /pkg/fractile.deb >/tmp/apt2.log 2>&1 || { tail -30 /tmp/apt2.log; exit 1; }
su u -s /bin/bash -c "
set -euo pipefail
export XDG_RUNTIME_DIR=/tmp/rt-u FRACTILE_NO_LOGIND=1
Xvfb :6 -screen 0 1280x800x24 >/dev/null 2>&1 & sleep 1
export DISPLAY=:6
fractiled >/tmp/d2.log 2>&1 & sleep 1.5
fractile list-snapshots | grep -q container-test && echo \"   snapshot still listed after reinstall\"
[ \"\$(fractile config get general.show_help)\" = false ] && echo \"   setting kept after reinstall\"
fractile purge-data --dry-run
fractile purge-data </dev/null && { echo \"purge without --yes or a terminal must refuse\"; exit 1; } || true
fractile purge-data --yes
sleep 0.5
for f in ~/.config/fractile ~/.local/share/fractile ~/.local/state/fractile ~/.config/autostart/io.fractile.Fractile.desktop; do
  [ -e \"\$f\" ] && { echo \"left behind after purge-data: \$f\"; exit 1; }
done
[ -S \"\$XDG_RUNTIME_DIR/fractile/fractile.sock\" ] && { echo \"service still running after purge-data\"; exit 1; }
echo \"   purge-data stopped the service and removed config, data, logs and the autostart entry\"
"
apt-get purge -y -qq fractile >/dev/null 2>&1
echo "== purged; lifecycle complete"
'
