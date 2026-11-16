#!/usr/bin/env bash
# The arm64 .deb installed with apt into an arm64 Ubuntu 24.04 userland and run
# in an X session there, all aarch64 code through user-mode QEMU under proot
# (no binfmt registration, nothing on the host changes). The outer container is
# Ubuntu 26.04 for proot 5.4 (5.1 in 24.04 does not translate statx paths);
# PROOT_NO_SECCOMP=1 because proot 5.4's seccomp mode exits 182 with qemu.
# apt runs with --no-install-recommends: systemd (a Recommends of fractile)
# aborts proot in its own postinst; qt6-qpa-plugins, which recommends would
# bring, is named instead.
#   bash tests/cross/aarch64-proot-session.sh
# Needs packaging/build/fractile_<v>-1_arm64.deb (FRACTILE_TARGET=aarch64-unknown-linux-gnu
# packaging/debian/build-deb.sh, or tests/cross/aarch64-in-container.sh).
# Output: tests/evidence/aarch64-proot-session.json
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
deb=$(ls "$root"/packaging/build/fractile_*_arm64.deb | head -1)
base=ubuntu-base-24.04.5-base-arm64.tar.gz
sum=a91d5a93010193712d346d761372b7c9db6dfcf093893161c64ca107f05914f2
docker run --rm --platform linux/amd64 -e PROOT_NO_SECCOMP=1 -v "$deb:/in/fractile.deb:ro" -v "$root/tests/evidence:/evidence" \
  -v fractile-cross-rootfs:/cache -e BASE=$base -e SUM=$sum ubuntu:26.04 bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends proot qemu-user curl ca-certificates python3 util-linux >/tmp/apt.log 2>&1 || { tail -20 /tmp/apt.log; exit 2; }
[ -s /cache/$BASE ] || curl -fsSL -o /cache/$BASE https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/$BASE
echo "$SUM  /cache/$BASE" | sha256sum -c - >/dev/null
mkdir /r && tar -xzf /cache/$BASE -C /r && chmod 1777 /r/tmp
cp /etc/resolv.conf /r/etc/resolv.conf; cp /in/fractile.deb /r/tmp/fractile.deb
A() { proot -q qemu-aarch64 -r /r -b /dev -b /proc -b /sys -w / "$@"; }
results=/tmp/results; : > $results
check() { printf "%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$4" >> $results; echo "$( [ "$3" = 1 ] && echo PASS || echo FAIL ) $1 $2 :: $4"; }

A -0 /usr/bin/uname -m > /tmp/m; m=$(cat /tmp/m)
check AP0 "the userland is aarch64 (uname -m inside proot)" "$([ "$m" = aarch64 ] && echo 1 || echo 0)" "$m; $(A /usr/bin/dpkg --print-architecture)"

A -0 /bin/bash -c "apt-get update -q >/tmp/u.log 2>&1 && apt-get install -y -q --no-install-recommends /tmp/fractile.deb xvfb xauth openbox x11-apps x11-utils qt6-qpa-plugins >/tmp/i.log 2>&1" && inst=0 || inst=$?
[ $inst = 0 ] || tail -20 /r/tmp/i.log
v=$(A /usr/bin/dpkg-query -W -f="\${Version} \${Architecture}" fractile 2>/dev/null || true)
check AP1 "apt installs the arm64 .deb with its dependencies from the Ubuntu ports archive (--no-install-recommends: systemd, a Recommends, aborts proot in its postinst)" "$([ $inst = 0 ] && [ "$v" = "0.9.0-1 arm64" ] && echo 1 || echo 0)" "apt rc $inst; fractile $v; $(A /usr/bin/dpkg-query -W -f="python3-pyqt6 \${Version} \${Architecture}" python3-pyqt6 2>/dev/null)"

# the session, as an ordinary user (uid 4242): Xvfb, openbox, xeyes, fractiled
cat > /r/tmp/session.sh <<"SH"
set -u
export HOME=/tmp/h XDG_RUNTIME_DIR=/tmp/rt DISPLAY=:7 FRACTILE_NO_LOGIND=1
mkdir -p $HOME; mkdir -m 700 -p $XDG_RUNTIME_DIR
Xvfb :7 -screen 0 1280x800x24 >/tmp/x.log 2>&1 & sleep 8
openbox >/tmp/ob.log 2>&1 & sleep 6
xeyes -geometry 300x200+500+400 >/dev/null 2>&1 & sleep 6
fractiled >/tmp/d.log 2>&1 & sleep 10
fractile --version > /tmp/ver.txt 2>&1
fractile --json status > /tmp/status.txt 2>&1
fractile --json list-windows > /tmp/before.json 2>/tmp/lw.err
w=$(python3 -c "import json; print([x for x in json.load(open(\"/tmp/before.json\")) if (x.get(\"wm_class\") or \"\").lower()==\"xeyes\"][0][\"id\"])")
fractile place --grid 2x2 --cells 0,0,0,0 --window $w > /tmp/place.txt 2>&1; echo $? > /tmp/place.rc
sleep 3
fractile --json list-windows > /tmp/after.json 2>&1
xwininfo -id ${w#x11:} > /tmp/xwininfo.txt 2>&1
ls -l $XDG_RUNTIME_DIR/fractile > /tmp/rt.txt 2>&1
# start the UI as a user would, on the X display; it either stays up and
# attaches, or hands over to a UI already running (the service may have started
# a tray-only one, which leaves again when no tray exists) and exits 0
fractile-ui > /tmp/ui.log 2>&1 & uip=$!
sleep 60
if kill -0 $uip 2>/dev/null; then echo alive > /tmp/ui.state; else wait $uip; echo "exited $?" > /tmp/ui.state; fi
ps -eo args > /tmp/ps.txt 2>&1
fractile --json status > /tmp/status2.txt 2>&1
kill $uip 2>/dev/null || true
fractile quit > /dev/null 2>&1 || true
sleep 2
kill $(jobs -p) 2>/dev/null || true   # proot waits for every traced process
SH
setpriv --reuid 4242 --regid 4242 --clear-groups proot -q qemu-aarch64 -r /r -b /dev -b /proc -b /sys -w / /bin/bash /tmp/session.sh || true
T=/r/tmp
st=$(cat $T/status.txt 2>/dev/null | tr "\n" " ")
check AP2 "fractiled (aarch64) starts as a plain user under X11/openbox and reports window control" "$(grep -qE "\"window_control\": ?true" $T/status.txt 2>/dev/null && echo 1 || echo 0)" "$(cat $T/ver.txt 2>/dev/null); $(echo "$st" | cut -c1-300)"
geo=$(python3 - <<PY
import json
try:
    b=[x for x in json.load(open("$T/before.json")) if (x.get("wm_class") or "").lower()=="xeyes"][0]["frame"]
    a=[x for x in json.load(open("$T/after.json")) if (x.get("wm_class") or "").lower()=="xeyes"][0]["frame"]
    ok = a != b and a["x"] < 64 and a["y"] < 64 and 560 <= a["w"] <= 660 and 340 <= a["h"] <= 420
    print(("1" if ok else "0") + "\tbefore %s after %s" % (b, a))
except Exception as e:
    print("0\t" + repr(e))
PY
)
xw=$(grep -E "Absolute upper-left|Width|Height" $T/xwininfo.txt 2>/dev/null | tr -s " " | tr "\n" ";" || true)
check AP3 "fractile place --grid 2x2 --cells 0,0,0,0 moves a real X window into the top-left quarter (read back by fractile and by xwininfo)" "${geo%%	*}" "rc $(cat $T/place.rc 2>/dev/null); ${geo#*	}; xwininfo: $xw"
check AP4 "the control socket is mode 0600 (srw-------)" "$(grep -q "^srw------- .*fractile" $T/rt.txt 2>/dev/null && echo 1 || echo 0)" "$(grep fractile $T/rt.txt 2>/dev/null | tr -s " " | cut -c1-160)"
uierr=$(cat $T/ui.log $T/d.log 2>/dev/null | grep -iE "Traceback|Error" | head -2 | tr "\n" " " || true)
uiproc=$(grep -m1 "^/usr/bin/qemu-aarch64 .*fractile-ui" $T/ps.txt 2>/dev/null | cut -c1-140 || true)
uist=$(cat $T/ui.state 2>/dev/null || true)
check AP5 "fractile-ui (arm64 Python and PyQt6 on X11) runs and the service reports it attached, with no Python errors" "$([ -n "$uiproc" ] && [ -z "$uierr" ] && { [ "$uist" = alive ] || [ "$uist" = "exited 0" ]; } && grep -qE "\"ui_attached\": ?true" $T/status2.txt 2>/dev/null && echo 1 || echo 0)" "ui after 60 s: ${uist:-?}; process: ${uiproc:-none}; $(grep -oE "\"ui_attached\": ?[a-z]+" $T/status2.txt 2>/dev/null || true); errors: ${uierr:-none}"

python3 - <<PY
import json, datetime
rows = [l.rstrip("\n").split("\t") for l in open("/tmp/results")]
res = [{"id": r[0], "name": r[1], "ok": r[2] == "1", "detail": r[3][:600]} for r in rows]
print(str(sum(1 for r in res if r["ok"])) + "/" + str(len(res)) + " checks passed")
json.dump({"suite": "aarch64-proot-session", "when": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "environment": "amd64 ubuntu:26.04 container (proot 5.4, qemu-user 10.2); arm64 ubuntu-base 24.04.5 rootfs run with proot -q qemu-aarch64 (user-mode emulation, no binfmt); Xvfb + openbox, all aarch64; x86_64 host",
           "results": res, "ok": all(r["ok"] for r in res)}, open("/evidence/aarch64-proot-session.json", "w"), indent=2)
PY
for f in d.log status.txt status2.txt place.txt lw.err ui.log ps.txt; do echo "--- $f"; tail -5 $T/$f 2>/dev/null; done
'
