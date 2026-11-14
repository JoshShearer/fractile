#!/usr/bin/env bash
# Install Fractile into a staging root. Used by the .deb builder and the
# Arch PKGBUILD so both ship the same file layout.
#
#   packaging/install.sh <destdir> [prefix=/usr] [bindir-with-release-binaries=target/release]
#
# Layout:
#   $prefix/bin/{fractiled,fractile,fractile-ui}
#   $prefix/lib/fractile/fractile_ui/            (Python UI package)
#   $prefix/lib/systemd/user/fractile.service
#   $prefix/share/applications/io.fractile.Fractile.desktop
#   $prefix/share/icons/hicolor/scalable/apps/io.fractile.Fractile.svg
#   $prefix/share/metainfo/io.fractile.Fractile.metainfo.xml
#   $prefix/share/gnome-shell/extensions/fractile@fractile.io/   (GNOME integration)
#   $prefix/share/kwin/scripts/fractile/                         (KDE Plasma integration)
#   $prefix/share/doc/fractile/...
# Nothing is enabled at install time: the GNOME extension and the KWin
# script are only made available; each user enables them (first-run setup
# explains how). Login start is also the user's choice (first-run setup or
# Settings), per user, via `systemctl --user`.
set -euo pipefail
dest=${1:?destdir}; prefix=${2:-/usr}; bins=${3:-target/release}
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
for b in fractiled fractile; do
  [[ -x $bins/$b ]] || { echo "missing $bins/$b (run: cargo build --release --locked)" >&2; exit 1; }
done
P="$dest$prefix"
install -Dm755 "$bins/fractiled" "$P/bin/fractiled"
install -Dm755 "$bins/fractile" "$P/bin/fractile"
# The packaged UI runs on the distribution's Python, where the PyQt6 package
# is installed. `#!/usr/bin/env python3` would take the first python3 on the
# session's PATH, and version managers put theirs first (Omarchy's mise
# shims: a Python without PyQt6, so the UI did not start).
python=${FRACTILE_PYTHON:-/usr/bin/python3}
sed "1s|^#!.*python3.*\$|#!$python|" ui/fractile-ui > "$dest.fractile-ui.tmp"
install -Dm755 "$dest.fractile-ui.tmp" "$P/bin/fractile-ui"; rm -f "$dest.fractile-ui.tmp"
for f in ui/fractile_ui/*.py; do install -Dm644 "$f" "$P/lib/fractile/fractile_ui/$(basename "$f")"; done
install -Dm644 ui/fractile_ui/data/io.fractile.Fractile.svg "$P/lib/fractile/fractile_ui/data/io.fractile.Fractile.svg"
# The unit hard-codes /usr/bin; rewrite for other prefixes.
sed "s#/usr/bin/#$prefix/bin/#g" packaging/systemd/fractile.service > "$dest.fractile.service.tmp"
install -Dm644 "$dest.fractile.service.tmp" "$P/lib/systemd/user/fractile.service"; rm -f "$dest.fractile.service.tmp"
install -Dm644 packaging/xdg/io.fractile.Fractile.desktop "$P/share/applications/io.fractile.Fractile.desktop"
install -Dm644 packaging/icons/io.fractile.Fractile.svg "$P/share/icons/hicolor/scalable/apps/io.fractile.Fractile.svg"
ext=adapters/gnome-shell/fractile@fractile.io
for f in "$ext"/*; do install -Dm644 "$f" "$P/share/gnome-shell/extensions/fractile@fractile.io/$(basename "$f")"; done
(cd adapters/kwin/fractile && find . -type f) | while read -r f; do
  install -Dm644 "adapters/kwin/fractile/$f" "$P/share/kwin/scripts/fractile/${f#./}"
done
install -Dm644 packaging/metainfo/io.fractile.Fractile.metainfo.xml "$P/share/metainfo/io.fractile.Fractile.metainfo.xml"
for d in README.md CHANGELOG.md docs/*.md docs/*/*.md; do
  [[ -f $d ]] && install -Dm644 "$d" "$P/share/doc/fractile/$d"
done
[[ -f LICENSE ]] && install -Dm644 LICENSE "$P/share/licenses/fractile/LICENSE"
echo "installed into $dest$prefix"
