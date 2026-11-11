#!/usr/bin/env bash
# Create packaging/arch/fractile-<version>.tar.gz from the working tree
# (tracked and new files, honouring .gitignore) for a local makepkg.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
v=$(sed -n 's/^version = "\(.*\)"/\1/p' Cargo.toml | head -1)
out="$root/packaging/arch/fractile-$v.tar.gz"
git ls-files -co --exclude-standard -z | grep -zv '^packaging/arch/fractile-' | tar --null -T - --transform "s#^#fractile-$v/#" -czf "$out"
echo "$out"
