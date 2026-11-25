# Signing key and publishing

How to create the key that signs Fractile's packages and repositories, and how
to build and publish the signed apt and pacman repositories. Every command here
was run with GnuPG 2.4.4 on Pop!_OS 24.04 (with a throwaway key, 2026-11-19).
The scripts never upload anything; publishing is the last, manual step.

## 1. Create the key (once)

Use a key that is only for signing Fractile releases, not your personal key.

```sh
gpg --quick-gen-key "Fractile Archive Signing Key <you@your-domain>" ed25519 sign 3y
```

gpg asks for a passphrase; use a strong one. This makes one ed25519 key that
can sign and certify (`[SC]`), valid for 3 years.

Why not the usual "offline primary key with a signing subkey": debsig-verify
0.29 (Ubuntu 24.04) cannot verify a `.deb` signed by a subkey, under any
policy (checked: it looks the policy up by the subkey and then fails to find
the subkey in the keyring). apt and pacman accept subkeys, but the per-`.deb`
signature would not verify. A primary key that signs is also how distribution
archive keys are commonly set up.

Write down the fingerprint; everything below uses it:

```sh
gpg -K --keyid-format long "Fractile Archive Signing Key"
FPR=$(gpg --list-secret-keys --with-colons "Fractile Archive Signing Key" | awk -F: '/^fpr/{print $10; exit}')
echo "$FPR"
```

## 2. Back it up and keep the revocation certificate (once)

gpg already wrote a revocation certificate to
`~/.gnupg/openpgp-revocs.d/$FPR.rev`. Copy it, and a backup of the secret key,
to offline storage (an encrypted USB stick, a password manager's file vault):

```sh
gpg --armor --export-secret-keys "$FPR" > fractile-archive-secret.asc   # asks for the passphrase
cp ~/.gnupg/openpgp-revocs.d/"$FPR".rev fractile-archive-revoke.rev
gpg --armor --export "$FPR" > fractile-archive.asc                      # public key, safe to share
```

Do not commit `fractile-archive-secret.asc` or the `.rev` file anywhere. If the
key is ever lost or leaked, import the `.rev` file (`gpg --import
fractile-archive-revoke.rev`), publish the revoked public key, make a new key and
republish the repositories.

Publish the fingerprint where users will see it (the README, the website) so
they can check the key they download.

## 3. Build the signed apt repository

```sh
sudo apt install apt-utils debsigs      # apt-ftparchive; debsigs signs each .deb
bash packaging/debian/build-deb.sh      # fractile_*.deb and fractile-dbgsym_*.deb in packaging/build
FRACTILE_TARGET=aarch64-unknown-linux-gnu bash packaging/debian/build-deb.sh   # optional arm64 (needs the cross toolchain)
FRACTILE_GPG_KEY=$FPR bash packaging/debian/make-apt-repo.sh   # writes packaging/build/apt
```

Without debsigs the repository is still signed, but the `.deb`s are not signed
one by one; the script says so. Check the result:

```sh
gpgv --keyring packaging/build/apt/fractile-archive-keyring.gpg packaging/build/apt/dists/stable/InRelease
bash tests/packaging/apt-repo-in-container.sh   # the same checks with a throwaway key
```

## 4. Build the signed pacman repository

On an Arch machine (`repo-add` comes with pacman), with the secret key imported
there (`gpg --import fractile-archive-secret.asc`):

```sh
cd packaging/arch && ./make-srctar.sh && makepkg     # fractile-*.pkg.tar.zst and fractile-debug-*.pkg.tar.zst
FRACTILE_GPG_KEY=$FPR ./make-pacman-repo.sh ../build/pacman fractile-*.pkg.tar.zst
```

## 5. Publish

Upload `packaging/build/apt/` and `packaging/build/pacman/` as plain files to
any HTTPS static host (GitHub Pages, an S3 or R2 bucket, your own web server).
Then tell users:

Ubuntu / Pop!_OS:

```sh
sudo install -m644 fractile-archive-keyring.gpg /usr/share/keyrings/   # from <url>/fractile-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/fractile-archive-keyring.gpg] <url>/apt stable main" |
  sudo tee /etc/apt/sources.list.d/fractile.list
sudo apt update && sudo apt install fractile
```

Arch:

```sh
sudo pacman-key --add fractile.gpg          # from <url>/pacman/fractile.gpg
sudo pacman-key --lsign-key <FPR>
printf '\n[fractile]\nSigLevel = Required\nServer = <url>/pacman\n' | sudo tee -a /etc/pacman.conf
sudo pacman -Sy fractile
```

## 6. AUR

The AUR does not use this key. It needs an account on aur.archlinux.org with an
SSH public key added. Before submitting, replace the PKGBUILD's local `source`
with the release tarball URL and run `updpkgsums`. Optionally sign the tarball
(`gpg --detach-sign --armor -u "$FPR" fractile-0.9.1.tar.gz`), publish the
`.asc` next to it, and add it to `source` with `validpgpkeys=('<FPR>')`.

## 7. Renew before it expires

```sh
gpg --quick-set-expire "$FPR" 2y
```

Then rebuild the repositories (step 3 and 4 export the renewed public key) and
publish again. Users with the old keyring file must download the new one; an
expired key makes apt and pacman refuse the repository.
