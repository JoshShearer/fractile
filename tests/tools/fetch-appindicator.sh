#!/usr/bin/env bash
# Fetch the Ubuntu AppIndicator GNOME Shell extension (the tray host most
# GNOME users have) for the tray tests, without root: `apt-get download`
# plus `dpkg-deb -x` into a cache directory. Prints the extension directory.
#   fetch-appindicator.sh [cache-dir]
set -euo pipefail
cache=${1:-${XDG_CACHE_HOME:-$HOME/.cache}/fractile-tests/appindicator}
uuid=ubuntu-appindicators@ubuntu.com
ext="$cache/x/usr/share/gnome-shell/extensions/$uuid"
if [[ ! -f $ext/schemas/gschemas.compiled ]]; then
  mkdir -p "$cache"
  (cd "$cache" && rm -f ./*.deb && apt-get download gnome-shell-extension-appindicator >/dev/null)
  dpkg-deb -x "$cache"/gnome-shell-extension-appindicator_*.deb "$cache/x"
  mkdir -p "$ext/schemas"
  cp "$cache"/x/usr/share/glib-2.0/schemas/*.gschema.xml "$ext/schemas/"
  glib-compile-schemas "$ext/schemas"
fi
echo "$ext"
