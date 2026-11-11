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
#   $prefix/share/doc/fractile/...
# Nothing is enabled at install time: login start is the user's choice
# (first-run setup or Settings), per user, via `systemctl --user`.
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
install -Dm755 ui/fractile-ui "$P/bin/fractile-ui"
for f in ui/fractile_ui/*.py; do install -Dm644 "$f" "$P/lib/fractile/fractile_ui/$(basename "$f")"; done
install -Dm644 ui/fractile_ui/data/io.fractile.Fractile.svg "$P/lib/fractile/fractile_ui/data/io.fractile.Fractile.svg"
# The unit hard-codes /usr/bin; rewrite for other prefixes.
sed "s#/usr/bin/#$prefix/bin/#g" packaging/systemd/fractile.service > "$dest.fractile.service.tmp"
install -Dm644 "$dest.fractile.service.tmp" "$P/lib/systemd/user/fractile.service"; rm -f "$dest.fractile.service.tmp"
install -Dm644 packaging/xdg/io.fractile.Fractile.desktop "$P/share/applications/io.fractile.Fractile.desktop"
install -Dm644 packaging/icons/io.fractile.Fractile.svg "$P/share/icons/hicolor/scalable/apps/io.fractile.Fractile.svg"
install -Dm644 packaging/metainfo/io.fractile.Fractile.metainfo.xml "$P/share/metainfo/io.fractile.Fractile.metainfo.xml"
for d in README.md CHANGELOG.md docs/*.md docs/*/*.md; do
  [[ -f $d ]] && install -Dm644 "$d" "$P/share/doc/fractile/$d"
done
[[ -f LICENSE ]] && install -Dm644 LICENSE "$P/share/licenses/fractile/LICENSE"
echo "installed into $dest$prefix"
