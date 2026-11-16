#!/usr/bin/env bash
# Build a signed pacman repository from built packages (SRS 9: "If offering
# official pacman repo, sign packages and maintain proper metadata").
#   FRACTILE_GPG_KEY=<fingerprint> packaging/arch/make-pacman-repo.sh <outdir> <pkg.tar.zst>...
# Needs gpg with that secret key and repo-add (pacman). Each package gets a
# detached signature (<pkg>.sig), the database is signed (repo-add --sign
# --verify), and the public key is exported as fractile.gpg. Users add:
#   [fractile]
#   SigLevel = Required
#   Server = <url>
# after `pacman-key --add fractile.gpg && pacman-key --lsign-key <fingerprint>`.
# Publishing the directory is left to the maintainer; this script uploads nothing.
set -euo pipefail
key=${FRACTILE_GPG_KEY:?set FRACTILE_GPG_KEY to the signing key fingerprint}
out=${1:?output directory}; shift
(( $# )) || { echo "no packages given" >&2; exit 2; }
gpg --batch --list-secret-keys "$key" >/dev/null 2>&1 || { echo "no secret key $key in gpg" >&2; exit 2; }
mkdir -p "$out"
for p in "$@"; do
  cp "$p" "$out/"
  gpg --batch --yes --default-key "$key" --detach-sign --use-agent -o "$out/$(basename "$p").sig" "$out/$(basename "$p")"
done
cd "$out"
GPGKEY=$key repo-add --sign --key "$key" --verify fractile.db.tar.zst $(for p in "$@"; do basename "$p"; done)
gpg --batch --yes --armor --export "$key" > fractile.gpg
echo "repository: $out"
