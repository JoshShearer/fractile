#!/usr/bin/env bash
# Signed pacman repository and the Arch debug package (SRS 9), in a clean
# archlinux container with a throwaway key made for the run (nothing is
# published, no real key is used):
#   bash tests/packaging/pacman-repo-in-container.sh
# Output: tests/evidence/pacman-repo.json
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
tarball=$("$root/packaging/arch/make-srctar.sh")
docker run --rm -v "$root/packaging/arch:/in:ro" -v "$tarball:/tarball/$(basename "$tarball"):ro" -v "$root/tests/evidence:/evidence" archlinux:latest bash -euo pipefail -c '
pacman -Syu --noconfirm --needed base-devel rust python-pyqt6 gdb python >/tmp/pacman.log 2>&1 || { tail -20 /tmp/pacman.log; exit 1; }
useradd -m builder
mkdir /build && cp /in/PKGBUILD /tarball/* /build/ && chown -R builder /build
su builder -s /bin/bash -c "cd /build && makepkg --noconfirm >/tmp/makepkg.log 2>&1" || { tail -40 /tmp/makepkg.log; exit 1; }
ls /build/*.pkg.tar.zst
results=/tmp/results; : > $results
check() { printf "%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$4" >> $results; echo "$( [ "$3" = 1 ] && echo PASS || echo FAIL ) $1 $2 :: $4"; }

main=$(ls /build/fractile-0*.pkg.tar.zst /build/fractile-[0-9]*.pkg.tar.zst 2>/dev/null | head -1)
dbg=$(ls /build/fractile-debug-*.pkg.tar.zst 2>/dev/null | head -1)
[ -n "$dbg" ] && n=$(bsdtar -tf "$dbg" | grep -c "usr/lib/debug/.build-id/.*\.debug$") || n=0
check P0 "makepkg builds fractile and, from the debug info, fractile-debug" "$([ -n "$main" ] && [ "$n" -ge 2 ] && echo 1 || echo 0)" "$(basename "${main:-none}"); $(basename "${dbg:-none}") with $n .debug files"

export GNUPGHOME=/tmp/gnupg; mkdir -m 700 $GNUPGHOME
gpg --batch --quiet --pinentry-mode loopback --passphrase "" --quick-gen-key "Fractile test archive (throwaway) <test@fractile.invalid>" ed25519 sign 1d
FPR=$(gpg --batch --with-colons --list-secret-keys | awk -F: "/^fpr/{print \$10; exit}")
cp /in/make-pacman-repo.sh /tmp/
FRACTILE_GPG_KEY=$FPR bash /tmp/make-pacman-repo.sh /repo "$main" "$dbg" > /tmp/repo.log 2>&1 || { cat /tmp/repo.log; exit 3; }
ls /repo
sigs=$(ls /repo/*.sig | wc -l)
gpg --batch --verify /repo/fractile.db.tar.zst.sig /repo/fractile.db.tar.zst > /tmp/v.log 2>&1 && dbv=1 || dbv=0
check P1 "each package has a detached signature and the repository database is signed (repo-add --sign --verify)" "$([ "$sigs" -ge 3 ] && [ $dbv = 1 ] && echo 1 || echo 0)" "$sigs signatures: $(cd /repo && ls *.sig | tr "\n" " "); database signature: $(grep -o "Good signature.*" /tmp/v.log | head -1)"

pacman-key --init >/dev/null 2>&1
pacman-key --add /repo/fractile.gpg >/dev/null 2>&1
pacman-key --lsign-key "$FPR" >/dev/null 2>&1
cp /etc/pacman.conf /etc/pacman.conf.orig
printf "\n[fractile]\nSigLevel = Required\nServer = file:///repo\n" >> /etc/pacman.conf
pacman -Sy --noconfirm > /tmp/sy.log 2>&1 && sy=0 || sy=$?
pacman -S --noconfirm fractile fractile-debug > /tmp/s.log 2>&1 && s=0 || s=$?
v=$(pacman -Q fractile 2>/dev/null || true)
check P2 "with SigLevel = Required and the key locally signed, pacman syncs the repository and installs fractile and fractile-debug from it" "$([ $sy = 0 ] && [ $s = 0 ] && [ -n "$v" ] && echo 1 || echo 0)" "sync rc $sy, install rc $s; $v; $(pacman -Q fractile-debug 2>/dev/null)"

gdb -batch -ex "info line fractiled::main" /usr/bin/fractiled > /tmp/gdb.log 2>&1 || true
stripped=$(readelf -S /usr/bin/fractiled | grep -c "\.debug_info" || true)
check P3 "the installed binary has no debug info of its own and fractile-debug gives gdb its source lines" "$(grep -qE "^Line [0-9]+ of" /tmp/gdb.log && [ "$stripped" = 0 ] && echo 1 || echo 0)" "debug_info sections in /usr/bin/fractiled: $stripped; $(grep -m1 -v "^$" /tmp/gdb.log | cut -c1-160)"

# a package altered after signing is refused
pacman -R --noconfirm fractile-debug fractile >/dev/null 2>&1 || true
rm -rf /var/cache/pacman/pkg/fractile*
cp -a /repo /repo3
pk=$(ls /repo3/fractile-[0-9]*.pkg.tar.zst | head -1)
printf "X" | dd of="$pk" bs=1 seek=$(( $(stat -c %s "$pk") / 2 )) conv=notrunc 2>/dev/null
sed -i "s|file:///repo$|file:///repo3|" /etc/pacman.conf
pacman -Sy --noconfirm >/dev/null 2>&1 || true
pacman -S --noconfirm fractile > /tmp/t.log 2>&1 && t=0 || t=$?
check P4 "a package altered after signing is refused by pacman" "$([ $t != 0 ] && ! pacman -Q fractile >/dev/null 2>&1 && echo 1 || echo 0)" "install rc $t; $(grep -iE "error|invalid|corrupt|signature" /tmp/t.log | head -2 | tr "\n" " " | cut -c1-240)"

# a repository signed by a key the system does not trust is refused
sed -i "s|file:///repo3$|file:///repo|" /etc/pacman.conf
pacman-key --delete "$FPR" >/dev/null 2>&1
rm -f /var/lib/pacman/sync/fractile.*
pacman -Sy --noconfirm > /tmp/u.log 2>&1 && u=0 || u=$?
check P5 "with the signing key not trusted, pacman refuses the repository database" "$([ $u != 0 ] && echo 1 || echo 0)" "sync rc $u; $(grep -iE "error|unknown|signature|invalid" /tmp/u.log | head -2 | tr "\n" " " | cut -c1-240)"

python - <<PY
import json, datetime
rows = [l.rstrip("\n").split("\t") for l in open("/tmp/results")]
res = [{"id": r[0], "name": r[1], "ok": r[2] == "1", "detail": r[3][:600]} for r in rows]
ok = all(r["ok"] for r in res)
print(str(sum(1 for r in res if r["ok"])) + "/" + str(len(res)) + " checks passed")
json.dump({"suite": "pacman-repo", "when": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "environment": "archlinux:latest container; makepkg as an unprivileged user; throwaway ed25519 key generated for the run; file:// repository", "results": res, "ok": ok},
          open("/evidence/pacman-repo.json", "w"), indent=2)
PY
'
