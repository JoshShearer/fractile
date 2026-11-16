#!/usr/bin/env bash
# Build a signed apt repository from the .debs in packaging/build (SRS 9:
# "signed .deb package and optional apt repository").
#   FRACTILE_GPG_KEY=<fingerprint> packaging/debian/make-apt-repo.sh [outdir]
# Needs gpg with that secret key, apt-ftparchive (apt-utils) and, to sign
# each .deb as well, debsigs. Output (default packaging/build/apt):
#   pool/main/f/fractile/*.deb       (each signed with debsigs when available)
#   dists/stable/{Release,InRelease,Release.gpg}, main/binary-{amd64,arm64}/Packages{,.gz,.xz}
#   fractile-archive-keyring.gpg     (the public key, binary, for signed-by)
# Users add it with:
#   sudo install -m644 fractile-archive-keyring.gpg /usr/share/keyrings/
#   echo "deb [signed-by=/usr/share/keyrings/fractile-archive-keyring.gpg] <url> stable main" |
#     sudo tee /etc/apt/sources.list.d/fractile.list
# Publishing the directory is left to the maintainer; this script uploads nothing.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
key=${FRACTILE_GPG_KEY:?set FRACTILE_GPG_KEY to the signing key fingerprint}
out=${1:-$root/packaging/build/apt}
gpg --batch --list-secret-keys "$key" >/dev/null 2>&1 || { echo "no secret key $key in gpg" >&2; exit 2; }
shopt -s nullglob
debs=("$root"/packaging/build/fractile_*.deb "$root"/packaging/build/fractile-dbgsym_*.deb)
(( ${#debs[@]} )) || { echo "no .debs in packaging/build; run packaging/debian/build-deb.sh" >&2; exit 2; }
rm -rf "$out"; mkdir -p "$out/pool/main/f/fractile"
for d in "${debs[@]}"; do
  cp "$d" "$out/pool/main/f/fractile/"
done
if command -v debsigs >/dev/null; then
  for d in "$out"/pool/main/f/fractile/*.deb; do
    debsigs --sign=origin --default-key="$key" "$d"
  done
  signed_debs=yes
else
  echo "make-apt-repo: debsigs not installed; the .debs are not signed one by one (the repository is)" >&2
  signed_debs=no
fi
cd "$out"
archs=$(for d in pool/main/f/fractile/*.deb; do dpkg-deb -f "$d" Architecture; done | sort -u | tr '\n' ' ')
for a in $archs; do
  mkdir -p "dists/stable/main/binary-$a"
  apt-ftparchive --arch "$a" packages pool > "dists/stable/main/binary-$a/Packages"
  gzip -9nk "dists/stable/main/binary-$a/Packages"
  xz -9k "dists/stable/main/binary-$a/Packages"
done
apt-ftparchive -o APT::FTPArchive::Release::Origin=Fractile -o APT::FTPArchive::Release::Label=Fractile \
  -o APT::FTPArchive::Release::Suite=stable -o APT::FTPArchive::Release::Codename=stable \
  -o APT::FTPArchive::Release::Architectures="${archs% }" -o APT::FTPArchive::Release::Components=main \
  -o APT::FTPArchive::Release::Description="Fractile packages" release dists/stable > dists/stable/Release
gpg --batch --yes --default-key "$key" --clearsign -o dists/stable/InRelease dists/stable/Release
gpg --batch --yes --default-key "$key" -abs -o dists/stable/Release.gpg dists/stable/Release
gpg --batch --yes --export "$key" > fractile-archive-keyring.gpg
echo "repository: $out (architectures: ${archs% }; .debs signed one by one: $signed_debs)"
