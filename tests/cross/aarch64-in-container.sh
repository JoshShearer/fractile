#!/usr/bin/env bash
# aarch64 (PK-xx, SRS 5): cross-build Fractile for aarch64-unknown-linux-gnu in
# an amd64 Ubuntu 24.04 container, run the whole Rust test suite on aarch64
# under user-mode QEMU (qemu-aarch64 as cargo's runner: no binfmt or host
# changes), smoke-test the aarch64 service and CLI over their socket, and
# build the arm64 .deb.
#   bash tests/cross/aarch64-in-container.sh
# Output: tests/evidence/cross-aarch64.json, packaging/build/fractile_<v>-1_arm64.deb
# This is emulation on an x86_64 host, not aarch64 hardware.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
rust=${FRACTILE_RUST:-stable}
docker volume create fractile-cross-cargo >/dev/null; docker volume create fractile-cross-rustup >/dev/null
docker run --rm -e TRACE="${TRACE:-}" -v "$root:/src:ro" -v "$root/tests/evidence:/evidence" -v "$root/packaging/build:/out" \
  -v fractile-cross-cargo:/root/.cargo -v fractile-cross-rustup:/root/.rustup -e RUST="$rust" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" --platform linux/amd64 ubuntu:24.04 bash -euo pipefail -c '[ -n "${TRACE:-}" ] && set -x

export DEBIAN_FRONTEND=noninteractive
# arm64 libraries to link against (x11rb passes -lxcb; the binaries do not
# keep it, see ldd): Ubuntu keeps arm64 on ports.ubuntu.com.
sed -i "s/^Types: deb$/Types: deb\nArchitectures: amd64/" /etc/apt/sources.list.d/ubuntu.sources
printf "Types: deb\nURIs: http://ports.ubuntu.com/ubuntu-ports/\nSuites: noble noble-updates noble-security\nComponents: main universe\nArchitectures: arm64\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n" > /etc/apt/sources.list.d/ports.sources
dpkg --add-architecture arm64
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends ca-certificates curl build-essential gcc-aarch64-linux-gnu libc6-dev-arm64-cross binutils-aarch64-linux-gnu qemu-user dpkg-dev file python3 rsync libxcb1-dev:arm64 >/tmp/apt.log 2>&1 || { tail -20 /tmp/apt.log; exit 2; }
export PATH=/root/.cargo/bin:$PATH
command -v rustup >/dev/null || curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none >/dev/null
rustup toolchain install "$RUST" --profile minimal >/dev/null
rustup default "$RUST" >/dev/null
rustup target add --toolchain "$RUST" aarch64-unknown-linux-gnu >/dev/null
mkdir /work; rsync -a --exclude target --exclude packaging/build --exclude .git /src/ /work/
cd /work
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUNNER="qemu-aarch64 -L /usr/aarch64-linux-gnu"
export CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc AR_aarch64_unknown_linux_gnu=aarch64-linux-gnu-ar
T=aarch64-unknown-linux-gnu
rustc --version > /tmp/rustc.txt
set +e
cargo test --workspace --locked --target $T 2>&1 | tee /tmp/test.log | grep -E "^test result|FAILED|panicked" ; test_rc=${PIPESTATUS[0]}
[ "$test_rc" = 0 ] || { echo "== cargo test failed ($test_rc):"; grep -E "^error|^warning: unused|-->|^test .* FAILED|panicked" /tmp/test.log | head -40; }
FRACTILE_TARGET=$T bash packaging/debian/build-deb.sh > /tmp/deb.log 2>&1; deb_rc=$?
[ "$deb_rc" = 0 ] || { echo "== deb build failed ($deb_rc):"; grep -E "^error|-->" /tmp/deb.log | head -20; }
set -e
B=target/$T/release
Q="qemu-aarch64 -L /usr/aarch64-linux-gnu"
file $B/fractiled $B/fractile > /tmp/file.txt
$Q $B/fractiled --version > /tmp/dv.txt 2>&1 || true
$Q $B/fractile --version > /tmp/cv.txt 2>&1 || true
# The aarch64 service with no desktop: it starts, answers on its 0600 socket, and
# says it has no window control here.
# It refuses to run as root (a per-user service): run it as a plain user.
useradd -m -u 4242 tester
chmod 755 /work /work/target /work/target/$T /work/target/$T/release
U="setpriv --reuid=4242 --regid=4242 --clear-groups env HOME=/home/tester XDG_RUNTIME_DIR=/tmp/xdg"
mkdir -m 700 -p /tmp/xdg; chown tester: /tmp/xdg
$U $Q $B/fractiled > /tmp/daemon.log 2>&1 & dpid=$!
for i in $(seq 1 60); do [ -S /tmp/xdg/fractile/fractile.sock ] && break; sleep 0.5; done
$U $Q $B/fractile --json status > /tmp/status.json 2>&1 || true
stat -c %a /tmp/xdg/fractile/fractile.sock > /tmp/sockmode.txt 2>/dev/null || echo none > /tmp/sockmode.txt
$U $Q $B/fractile --json config get appearance.default_grid > /tmp/cfg.json 2>&1 || true
if $Q $B/fractiled > /tmp/root.log 2>&1; then echo "rc=0" >> /tmp/root.log; else echo "rc=$?" >> /tmp/root.log; fi
kill $dpid 2>/dev/null || true; wait $dpid 2>/dev/null || true
echo "== service log:"; tail -15 /tmp/daemon.log
deb=$(ls packaging/build/fractile_*_arm64.deb 2>/dev/null | head -1)
[ -n "$deb" ] && { cp "$deb" /out/; dpkg-deb -I "$deb" | grep -E "Architecture|Version|Depends" > /tmp/debinfo.txt; dpkg-deb --fsys-tarfile "$deb" | tar -xO ./usr/bin/fractiled > /tmp/fd; file /tmp/fd >> /tmp/debinfo.txt; chown "$HOST_UID:$HOST_GID" /out/$(basename "$deb"); }
python3 - "$test_rc" "$deb_rc" "${deb:-}" <<PY
import json, re, sys, datetime
r = lambda p: open(p).read().strip()
test_rc, deb_rc, deb = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
log = r("/tmp/test.log")
passed = sum(int(m) for m in re.findall(r"test result: ok\. (\d+) passed", log))
failed = sum(int(m) for m in re.findall(r"(\d+) failed", log))
status = r("/tmp/status.json")
try:
    st = json.loads(status)
except ValueError:
    st = {}
checks = []
rustc, files, dv, cv, sockmode, cfgout, debinfo, deblog, rootlog = (r(p) for p in ("/tmp/rustc.txt", "/tmp/file.txt", "/tmp/dv.txt", "/tmp/cv.txt", "/tmp/sockmode.txt", "/tmp/cfg.json", "/tmp/debinfo.txt", "/tmp/deb.log", "/tmp/root.log"))
nl = chr(10)
rootlog1 = rootlog.replace(nl, " ")
def check(i, name, ok, detail=""):
    checks.append({"id": i, "name": name, "ok": bool(ok), "detail": detail[:600]})
    print(("PASS" if ok else "FAIL"), i, name, "::", detail[:300])
check("AA01", "the workspace builds for aarch64-unknown-linux-gnu and every Rust test passes on aarch64 (user-mode QEMU)", test_rc == 0 and failed == 0 and passed > 0, f"{passed} passed, {failed} failed; {rustc}")
check("AA02", "the release binaries are aarch64 ELF and report their version", "ARM aarch64" in files and "0.9" in dv and "0.9" in cv, files.replace(nl, "; ") + "; " + dv + "; " + cv)
check("AA03", "the aarch64 service, run as a plain user with no desktop, answers the aarch64 CLI on a 0600 socket and reports no window control; run as root it refuses", sockmode == "600" and isinstance(st, dict) and st.get("window_control") is False and "refusing to run as root" in rootlog, f"socket mode {sockmode}; status {status[:300]}; config {cfgout[:100]}; as root: {rootlog1[:120]}")
check("AA04", "an arm64 .deb is built from the cross build", deb_rc == 0 and bool(deb) and "Architecture: arm64" in debinfo and "aarch64" in debinfo, debinfo.replace(nl, "; ") if deb else deblog[-300:])
ok = all(c["ok"] for c in checks)
n_ok = sum(1 for c in checks if c["ok"])
print(f"{n_ok}/{len(checks)} checks passed")
json.dump({"suite": "cross-aarch64", "when": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "environment": "amd64 ubuntu:24.04 container, gcc-aarch64-linux-gnu, qemu-aarch64 (user mode) as cargo runner; emulation, not aarch64 hardware",
           "results": checks, "ok": ok}, open("/evidence/cross-aarch64.json", "w"), indent=2)
sys.exit(0 if ok else 1)
PY
'
