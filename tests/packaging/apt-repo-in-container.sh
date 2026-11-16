#!/usr/bin/env bash
# Signed apt repository, signed .debs and the debug package (SRS 9), checked in
# an Ubuntu 24.04 container with a throwaway key made for the run (nothing is
# published, no real key is used):
#   bash tests/packaging/apt-repo-in-container.sh
# Needs packaging/build/fractile_<v>-1_amd64.deb and fractile-dbgsym_... (build-deb.sh).
# Output: tests/evidence/apt-repo.json
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
docker run --rm -v "$root:/src:ro" -v "$root/tests/evidence:/evidence" --platform linux/amd64 ubuntu:24.04 bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends gnupg apt-utils debsigs debsig-verify xz-utils dpkg-dev gdb python3 ca-certificates file binutils >/tmp/apt.log 2>&1 || { tail -20 /tmp/apt.log; exit 2; }
mkdir -p /work/packaging/debian /work/packaging/build
cp /src/packaging/debian/make-apt-repo.sh /work/packaging/debian/
cp /src/packaging/build/fractile_*_amd64.deb /src/packaging/build/fractile-dbgsym_*_amd64.deb /work/packaging/build/
export GNUPGHOME=/tmp/gnupg; mkdir -m 700 $GNUPGHOME
gpg --batch --quiet --pinentry-mode loopback --passphrase "" --quick-gen-key "Fractile test archive (throwaway) <test@fractile.invalid>" ed25519 sign 1d
FPR=$(gpg --batch --with-colons --list-secret-keys | awk -F: "/^fpr/{print \$10; exit}")
KEYID=${FPR: -16}
FRACTILE_GPG_KEY=$FPR bash /work/packaging/debian/make-apt-repo.sh /repo > /tmp/repo.log 2>&1 || { cat /tmp/repo.log; exit 3; }
cat /tmp/repo.log
results=/tmp/results; : > $results
check() { printf "%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$4" >> $results; echo "$( [ "$3" = 1 ] && echo PASS || echo FAIL ) $1 $2 :: $4"; }

# R1: the Release signature verifies with the published key only
gpgv --keyring /repo/fractile-archive-keyring.gpg /repo/dists/stable/InRelease > /tmp/gpgv.log 2>&1 && r1=1 || r1=0
check R1 "the repository Release file is signed (InRelease and Release.gpg) and verifies against the exported archive key" $r1 "$(tail -2 /tmp/gpgv.log | tr "\n" " ")"

# R2: apt with signed-by installs from it
install -m644 /repo/fractile-archive-keyring.gpg /usr/share/keyrings/fractile-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/fractile-archive-keyring.gpg] file:///repo stable main" > /etc/apt/sources.list.d/fractile.list
apt-get update > /tmp/upd.log 2>&1 && upd=0 || upd=$?
apt-get install -y -q fractile fractile-dbgsym > /tmp/inst.log 2>&1 && inst=0 || inst=$?
pol=$(apt-cache policy fractile | tr "\n" " " | sed "s/  */ /g")
v=$(dpkg-query -W -f="\${Version}" fractile 2>/dev/null || true)
[ $upd = 0 ] && [ $inst = 0 ] && [ -n "$v" ] && ! grep -qi "not signed\|NO_PUBKEY\|BADSIG" /tmp/upd.log && r2=1 || r2=0
check R2 "apt update accepts the signed repository (signed-by keyring) and apt installs fractile and fractile-dbgsym from it" $r2 "update rc $upd, install rc $inst; installed $v; $(echo "$pol" | cut -c1-200)"

# R3: each .deb carries a debsigs origin signature that debsig-verify accepts
mkdir -p /etc/debsig/policies/$KEYID /usr/share/debsig/keyrings/$KEYID
cp /repo/fractile-archive-keyring.gpg /usr/share/debsig/keyrings/$KEYID/debsig.gpg
cat > /etc/debsig/policies/$KEYID/fractile.pol <<POL
<?xml version="1.0"?>
<!DOCTYPE Policy SYSTEM "https://www.debian.org/debsig/1.0/policy.dtd">
<Policy xmlns="https://www.debian.org/debsig/1.0/">
  <Origin Name="Fractile" id="$KEYID" Description="Fractile packages"/>
  <Selection><Required Type="origin" File="debsig.gpg" id="$KEYID"/></Selection>
  <Verification MinOptional="0"><Required Type="origin" File="debsig.gpg" id="$KEYID"/></Verification>
</Policy>
POL
ok3=1; det3=""
for d in /repo/pool/main/f/fractile/*.deb; do
  if debsig-verify "$d" > /tmp/dv.log 2>&1; then det3="$det3 $(basename $d): verified;"; else ok3=0; det3="$det3 $(basename $d): $(tail -1 /tmp/dv.log);"; fi
done
# alter the package data, keep its original signature member
rm -rf /tmp/t; mkdir /tmp/t; cd /tmp/t
ar x /repo/pool/main/f/fractile/fractile_*_amd64.deb
members=$(ar t /repo/pool/main/f/fractile/fractile_*_amd64.deb | tr "\n" " ")
data=$(ls data.tar.*)
printf "X" | dd of=$data bs=1 seek=$(( $(stat -c %s $data) / 2 )) conv=notrunc 2>/dev/null
ar rc /tmp/tampered.deb $members; cd /
debsig-verify /tmp/tampered.deb > /tmp/dvt.log 2>&1 && { ok3=0; det3="$det3 tampered copy ACCEPTED"; } || det3="$det3 tampered copy rejected ($(tail -1 /tmp/dvt.log))"
check R3 "each .deb is signed (debsigs origin signature) and debsig-verify accepts it under a policy for the archive key; a tampered copy is rejected" $ok3 "$det3"

# R4: the debug package gives gdb the symbols of the stripped binaries
# with the debug package gdb maps main to a source line; with the debug
# directory hidden it cannot (the installed binary itself is stripped)
gdb -batch -ex "info line fractiled::main" /usr/bin/fractiled > /tmp/gdb.log 2>&1 || true
mkdir -p /tmp/nodebug
gdb -batch -ex "set debug-file-directory /tmp/nodebug" -ex "file /usr/bin/fractiled" -ex "info line fractiled::main" > /tmp/gdb0.log 2>&1 || true
stripped=$(file -L /usr/bin/fractiled | grep -c ", stripped" || true)
grep -qE "^Line [0-9]+ of \"" /tmp/gdb.log && ! grep -qE "^Line [0-9]+ of \"" /tmp/gdb0.log && [ "$stripped" = 1 ] && r4=1 || r4=0
check R4 "the installed binaries are stripped and fractile-dbgsym provides their debug info by build id (gdb maps fractiled::main to a source line only with it)" $r4 "stripped $stripped; with dbgsym: $(grep -m1 -E "Line|No line" /tmp/gdb.log | cut -c1-120); without: $(grep -m1 -v "^$" /tmp/gdb0.log | cut -c1-120)"

# R5: a tampered Release is refused
rm -rf /repo2; cp -a /repo /repo2
sed -i "0,/Packages/s/^ \([0-9a-f]\)/ f/" /repo2/dists/stable/InRelease
echo "deb [signed-by=/usr/share/keyrings/fractile-archive-keyring.gpg] file:///repo2 stable main" > /etc/apt/sources.list.d/fractile.list
apt-get update > /tmp/upd5.log 2>&1 || true
pat5="BADSIG|NOSPLIT|not signed|signature.*(invalid|could not be verified)|is not valid|Hash Sum mismatch"
grep -qiE "$pat5" /tmp/upd5.log && r5=1 || r5=0
check R5 "a repository whose signed Release was altered is refused by apt" $r5 "$(grep -iE "$pat5" /tmp/upd5.log | head -2 | tr "\n" " " | cut -c1-260)"

# R6: the same repository without signatures is refused
rm -f /repo2/dists/stable/InRelease /repo2/dists/stable/Release.gpg; cp /repo/dists/stable/Release /repo2/dists/stable/Release
apt-get update > /tmp/upd6.log 2>&1 || true
grep -qi "not signed" /tmp/upd6.log && r6=1 || r6=0
check R6 "the repository with its signatures removed is refused by apt (not signed)" $r6 "$(grep -i "not signed" /tmp/upd6.log | head -1 | cut -c1-200)"

python3 - <<PY
import json, datetime
rows = [l.rstrip("\n").split("\t") for l in open("/tmp/results")]
res = [{"id": r[0], "name": r[1], "ok": r[2] == "1", "detail": r[3][:600]} for r in rows]
ok = all(r["ok"] for r in res)
print(str(sum(1 for r in res if r["ok"])) + "/" + str(len(res)) + " checks passed")
json.dump({"suite": "apt-repo", "when": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "environment": "ubuntu:24.04 container; throwaway ed25519 key generated for the run; file:// repository", "results": res, "ok": ok},
          open("/evidence/apt-repo.json", "w"), indent=2)
PY
'
