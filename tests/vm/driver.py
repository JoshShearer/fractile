#!/usr/bin/env python3
"""T10 on a real desktop: an Ubuntu 24.04 VM (QEMU/KVM) with GNOME and GDM,
the Fractile .deb installed with apt, and real logins, logouts and reboots.
Test infrastructure only; runs inside tests/vm/Dockerfile.

  1. Base image (cached): Ubuntu's noble cloud image, checksum verified, plus
     ubuntu-desktop-minimal and the test apps, installed by cloud-init.
  2. Per run, a fresh copy-on-write disk on that base, and one session type
     (Wayland or X11) set for the test user, with GDM automatic login and
     timed login (so a log-out is followed by a new login). For plasma-x11 a
     second cached base adds kde-plasma-desktop (Plasma 5.27, as Kubuntu
     24.04), and GDM starts the Plasma X11 session; logging out is Plasma's
     own (ksmserver).
  3. apt installs the .deb; Fractile is started from the app menu entry and
     set up the way the first-run wizard and Settings do it; one real
     log-out/log-in loads the GNOME extension (Wayland needs that).
  4. 10 apps / 15 windows are opened and placed on a 4x4 grid.
  5. A real reboot (systemctl reboot): at the next automatic login, Fractile
     restores the layout. Checked: every window back where it was, no
     duplicates, the boot id changed.
  6. A real log-out (the session menu's action) and the timed re-login:
     checked the same way.

Evidence: /evidence/vm-ubuntu24.04-<session>.json and /evidence/vm/*.png.
Usage: driver.py --session wayland|x11|plasma-x11 --deb /src/packaging/build/fractile_*.deb
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CACHE = Path("/cache")
EVID = Path("/evidence")
IMG_URL = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
SUMS_URL = "https://cloud-images.ubuntu.com/noble/current/SHA256SUMS"
SSH_PORT = 2222
QMP = "/tmp/qmp.sock"
GUEST_TOOL = "/src/tests/vm/guest/vmctl.py"
# session -> (GDM session name, logind session type, the shell process)
SESSIONS = {"wayland": ("ubuntu", "wayland", "gnome-shell"), "x11": ("ubuntu-xorg", "x11", "gnome-shell"),
            "plasma-x11": ("plasma", "x11", "plasmashell")}
PLASMA_VERSIONS = "dpkg-query -W -f='${Package} ${Version}; ' plasma-workspace kwin-x11"
APT = ("sudo DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=60")
APPS_PKGS = ("ubuntu-desktop-minimal linux-generic gnome-terminal gnome-text-editor gnome-calculator "
             "gnome-system-monitor gnome-characters gnome-clocks baobab gnome-disk-utility gnome-font-viewer")

HOLD = 0
HOLD_AT = "login"
results: list[dict] = []
log_lines: list[str] = []


def say(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    log_lines.append(line)
    print(line, flush=True)


def check(tid: str, name: str, ok: bool, detail: str = "") -> bool:
    results.append({"id": tid, "name": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {tid} {name} :: {detail}", flush=True)
    if not ok:
        hold("fail")
    return ok


# ---------------------------------------------------------------- images

def ensure_cloud_image() -> Path:
    img = CACHE / "noble-server-cloudimg-amd64.img"
    if img.exists():
        return img
    sums = urllib.request.urlopen(SUMS_URL, timeout=60).read().decode()
    want = next(line.split()[0] for line in sums.splitlines() if line.endswith("noble-server-cloudimg-amd64.img"))
    part = img.with_suffix(".part")
    say(f"downloading {IMG_URL}")
    h = hashlib.sha256()
    with urllib.request.urlopen(IMG_URL, timeout=120) as r, open(part, "wb") as f:
        while chunk := r.read(1 << 20):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != want:
        part.unlink()
        raise SystemExit(f"checksum mismatch for the cloud image: {h.hexdigest()} != {want}")
    part.rename(img)
    say(f"cloud image verified (sha256 {want[:16]}...)")
    return img


def ssh_key() -> Path:
    k = CACHE / "id_ed25519"
    if not k.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k)], check=True)
    return k


def seed(path: Path) -> Path:
    pub = (ssh_key().with_suffix(".pub")).read_text().strip()
    ud = f"""#cloud-config
hostname: fractile-vm
users:
  - name: tester
    gecos: Test User
    groups: [sudo]
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys: ["{pub}"]
ssh_pwauth: false
package_update: true
# Ubuntu's firefox package only installs the snap, and the snap store is not
# reachable from the VM (it retries for 30 min, then fails the install). The
# tests do not use Firefox: keep it out.
write_files:
  - path: /etc/apt/preferences.d/no-snap-transitional
    content: |
      Package: firefox thunderbird
      Pin: version *
      Pin-Priority: -1
runcmd:
  - systemctl disable --now unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer || true
  - DEBIAN_FRONTEND=noninteractive apt-get install -y {APPS_PKGS} && touch /var/lib/fractile-apps-installed
  - DEBIAN_FRONTEND=noninteractive apt-get purge -y gnome-initial-setup update-notifier || true
  - mkdir -p /etc/dconf/profile /etc/dconf/db/local.d
  - printf 'user-db:user\\nsystem-db:local\\n' > /etc/dconf/profile/user
  - printf '[org/gnome/desktop/session]\\nidle-delay=uint32 0\\n[org/gnome/desktop/screensaver]\\nlock-enabled=false\\n[org/gnome/desktop/notifications]\\nshow-banners=false\\n' > /etc/dconf/db/local.d/00-fractile-test
  - dconf update
  - systemctl set-default graphical.target
  - test -e /var/lib/fractile-apps-installed && echo FRACTILE-PROVISION-OK > /dev/ttyS0 || echo FRACTILE-PROVISION-FAILED > /dev/ttyS0
power_state:
  mode: poweroff
  timeout: 60
"""
    d = Path("/tmp/seed")
    d.mkdir(exist_ok=True)
    (d / "user-data").write_text(ud)
    (d / "meta-data").write_text("instance-id: fractile-vm-1\nlocal-hostname: fractile-vm\n")
    subprocess.run(["cloud-localds", str(path), str(d / "user-data"), str(d / "meta-data")], check=True)
    return path


def qemu_cmd(disk: Path, seed_img: Path | None, serial: Path) -> list[str]:
    cmd = ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-smp", "4", "-m", "6144",
           "-drive", f"file={disk},if=virtio,cache=unsafe",
           "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{SSH_PORT}-:22", "-device", "virtio-net-pci,netdev=n0",
           "-device", "virtio-vga,edid=on,xres=1600,yres=1200", "-display", "none",
           "-qmp", f"unix:{QMP},server,nowait", "-serial", f"file:{serial}"]
    if seed_img:
        cmd += ["-drive", f"file={seed_img},if=virtio,format=raw"]
    return cmd


def ensure_base() -> Path:
    base = CACHE / "base-desktop.qcow2"
    if base.exists():
        return base
    img = ensure_cloud_image()
    tmp = CACHE / "base-desktop.part.qcow2"
    subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(img), str(tmp)], check=True)
    subprocess.run(["qemu-img", "resize", str(tmp), "24G"], check=True)
    sd = seed(Path("/tmp/seed.img"))
    say("provisioning the base image: Ubuntu desktop and the test apps (cloud-init)")
    serial = CACHE / "provision-serial.log"
    p = subprocess.Popen(qemu_cmd(tmp, sd, serial))
    try:
        p.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        p.kill()
        raise SystemExit(f"provisioning did not finish in 60 min; see {serial}")
    if "FRACTILE-PROVISION-OK" not in serial.read_text(errors="replace"):
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"provisioning failed (no success marker from cloud-init); see {serial}")
    tmp.rename(base)
    say("base image ready")
    return base


def ensure_plasma_base() -> Path:
    """The desktop base plus kde-plasma-desktop, GDM kept as the display
    manager, and the Plasma screen locker off (as the GNOME base turns its
    own off): an idle lock would make Fractile skip its captures."""
    out = CACHE / "base-plasma.qcow2"
    if out.exists():
        return out
    base = ensure_base()
    say("provisioning the Plasma image: kde-plasma-desktop on the desktop base")
    vm = VM(base, "plasma-provision")
    try:
        if not vm.wait_ssh(600):
            raise SystemExit("the Plasma provisioning VM did not come up")
        steps = [
            "echo 'gdm3 shared/default-x-display-manager select gdm3' | sudo debconf-set-selections",
            f"for i in 1 2 3; do {APT} update -q && {APT} install -y kde-plasma-desktop > /tmp/plasma.log 2>&1 && break; sleep 30; done; tail -3 /tmp/plasma.log",
            "test -e /usr/share/xsessions/plasma.desktop",
            "echo /usr/sbin/gdm3 | sudo tee /etc/X11/default-display-manager >/dev/null",
            "sudo systemctl disable sddm 2>/dev/null; sudo ln -sf /lib/systemd/system/gdm3.service /etc/systemd/system/display-manager.service",
            "printf '[Daemon]\\nAutolock=false\\nLockOnResume=false\\nTimeout=0\\n' | sudo tee /etc/xdg/kscreenlockerrc >/dev/null",
        ]
        for c in steps:
            p = vm.ssh(c, timeout=3600)
            if p.returncode != 0:
                raise SystemExit(f"Plasma provisioning step failed: {c} -> {p.returncode}: {(p.stdout + p.stderr).strip()[-600:]}")
        vm.stop()
    finally:
        if vm.p.poll() is None:
            vm.p.kill()
    subprocess.run(["mv", str(vm.disk), str(out)], check=True)
    say("Plasma image ready")
    return out


# ---------------------------------------------------------------- VM control

class VM:
    def __init__(self, base: Path, name: str):
        self.disk = Path(f"/tmp/{name}.qcow2")
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(self.disk)], check=True)
        self.serial = Path(f"/tmp/{name}-serial.log")
        self.seed = seed(Path("/tmp/run-seed.img"))
        self.p = subprocess.Popen(qemu_cmd(self.disk, self.seed, self.serial))
        self.ssh_key = ssh_key()

    def ssh(self, cmd: str, timeout=300, check_rc=False) -> subprocess.CompletedProcess:
        p = subprocess.run(["ssh", "-q", "-i", str(self.ssh_key), "-p", str(SSH_PORT), "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                            "-o", "ServerAliveInterval=5", "tester@127.0.0.1", cmd],
                           capture_output=True, text=True, timeout=timeout)
        if check_rc and p.returncode != 0:
            raise RuntimeError(f"ssh {cmd!r} -> {p.returncode}: {p.stderr.strip()[-500:]} {p.stdout.strip()[-500:]}")
        return p

    def scp(self, src: str, dst: str) -> None:
        subprocess.run(["scp", "-q", "-i", str(self.ssh_key), "-P", str(SSH_PORT), "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null", src, f"tester@127.0.0.1:{dst}"], check=True, timeout=120)

    def wait_ssh(self, timeout=600) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.p.poll() is not None:
                raise RuntimeError("QEMU exited")
            try:
                if self.ssh("true", timeout=15).returncode == 0:
                    return True
            except subprocess.TimeoutExpired:
                pass
            time.sleep(3)
        return False

    def wait_down(self, timeout=180) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.ssh("true", timeout=8).returncode != 0:
                    return True
            except subprocess.TimeoutExpired:
                return True
            time.sleep(2)
        return False

    def vmctl(self, cmd: str, timeout=600) -> dict:
        p = self.ssh(f"python3 /home/tester/vmctl.py {cmd}", timeout=timeout)
        try:
            return json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": f"vmctl {cmd} -> {p.returncode}: {p.stderr.strip()[-800:]}"}

    def screenshot(self, name: str) -> str:
        out = EVID / "vm" / f"{name}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(QMP)
            f = s.makefile("rw")
            f.readline()
            for msg in ({"execute": "qmp_capabilities"}, {"execute": "screendump", "arguments": {"filename": "/tmp/shot.ppm"}}):
                f.write(json.dumps(msg) + "\n")
                f.flush()
                while True:
                    r = json.loads(f.readline())
                    if "return" in r or "error" in r:
                        break
            s.close()
            from PIL import Image
            Image.open("/tmp/shot.ppm").save(out)
            return str(out).replace("/evidence/", "tests/evidence/")
        except Exception as ex:  # noqa: BLE001  a missing screenshot is not a test failure
            return f"(screenshot failed: {ex})"

    def key(self, qcode: str) -> bool:
        """A key press on the VM's virtual keyboard (real input, as a user
        would type it)."""
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(QMP)
            f = s.makefile("rw")
            f.readline()
            ok = True
            for msg in ({"execute": "qmp_capabilities"}, {"execute": "send-key", "arguments": {"keys": [{"type": "qcode", "data": qcode}]}}):
                f.write(json.dumps(msg) + "\n")
                f.flush()
                while True:
                    r = json.loads(f.readline())
                    if "return" in r or "error" in r:
                        ok = "return" in r
                        break
            s.close()
            return ok
        except OSError:
            return False

    def stop(self) -> None:
        if self.p.poll() is None:
            self.ssh("sudo systemctl poweroff", timeout=20)
            try:
                self.p.wait(90)
            except subprocess.TimeoutExpired:
                self.p.kill()


# ---------------------------------------------------------------- the run

def wait_session(vm: VM, want_type: str, prev_shell_boot: str | None = None, timeout=300) -> dict:
    """Wait until the user is logged in to the wanted session type and the
    Fractile service answers with window control."""
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        last = vm.vmctl("session", timeout=60)
        st = last.get("status") or {}
        if last.get("shell_pid") and last.get("graphical_session") == want_type and isinstance(st, dict) and st.get("window_control"):
            return last
        time.sleep(4)
    return last


def wait_restore_done(vm: VM, timeout=420, not_run: str | None = None) -> dict:
    """Until the newest restore run (other than `not_run`, an earlier one)
    has finished."""
    end = time.time() + timeout
    st = {}
    while time.time() < end:
        st = vm.vmctl("state", timeout=60)
        rs = st.get("restore") or {}
        runs = rs.get("recent_runs") or []
        if isinstance(rs, dict) and rs.get("running") is None and runs and runs[0].get("run_id") != not_run \
                and runs[0].get("state") in ("completed", "partial", "failed", "cancelled"):
            return st
        time.sleep(4)
    return st


def compare(before: list[dict], after: list[dict], tol=4) -> tuple[bool, list[str]]:
    """Per app: same number of windows, and each recorded frame matched by a
    distinct window within `tol` px."""
    notes, ok = [], True
    apps = sorted({b["app"] for b in before})
    for app in apps:
        want = [b["frame"] for b in before if b["app"] == app]
        have = [a["frame"] for a in after if a["app"] == app]
        if len(have) != len(want):
            ok = False
            notes.append(f"{app}: {len(have)} windows, expected {len(want)}")
            continue
        free = list(have)
        for fr in want:
            hit = next((h for h in free if h and fr and all(abs(h[k] - fr[k]) <= tol for k in ("x", "y", "w", "h"))), None)
            if hit is None:
                ok = False
                notes.append(f"{app}: no window at {fr}; have {free}")
            else:
                free.remove(hit)
    extra = [a for a in after if a["app"] not in apps and a["app"]]
    return ok, notes + ([f"other windows: {[a['app'] for a in extra]}"] if extra else [])


def gdm_and_session(vm: VM, session: str) -> None:
    xs = SESSIONS[session][0]
    # Timed login only: it logs in at boot and again after every log-out.
    # With automatic login enabled as well, the first run saw GDM 46 log
    # "conversation gdm-autologin started more than once" and the session's
    # shell killed (SIGKILL) within 2 s, in a loop.
    conf = ("[daemon]\\nAutomaticLoginEnable=False\\nTimedLoginEnable=True\\n"
            "TimedLogin=tester\\nTimedLoginDelay=5\\nWaylandEnable=true\\n[security]\\n[xdmcp]\\n[chooser]\\n[debug]\\n")
    acct = f"[User]\\nSession={xs}\\nXSession={xs}\\nSystemAccount=false\\n"
    # cloud-init creates the user with a locked password; the first run saw
    # GDM loop on it ("gdm-autologin started more than once"). A throwaway
    # password on this throwaway VM, used by nothing (login is timed).
    vm.ssh("echo tester:fractile-vm-test | sudo chpasswd", check_rc=True)
    vm.ssh(f"printf '{conf}' | sudo tee /etc/gdm3/custom.conf >/dev/null && printf '{acct}' | sudo tee /var/lib/AccountsService/users/tester >/dev/null", check_rc=True)


def run(session: str, deb: str) -> None:
    base = ensure_plasma_base() if session == "plasma-x11" else ensure_base()
    _xs, stype, shell = SESSIONS[session]
    plasma = session == "plasma-x11"
    vm = VM(base, f"run-{session}")
    try:
        if not check("V00", "the VM boots and answers over ssh", vm.wait_ssh(600)):
            return
        gdm_and_session(vm, session)
        vm.scp(deb, "/tmp/fractile.deb")
        vm.scp(GUEST_TOOL, "/home/tester/vmctl.py")
        rel = vm.ssh(f". /etc/os-release; echo $PRETTY_NAME; {PLASMA_VERSIONS if plasma else 'gnome-shell --version'}; uname -r").stdout.strip().replace("\n", "; ")
        inst = vm.ssh(
            # The VM's user-mode network has no IPv6, and archive.ubuntu.com
            # resolves to IPv6 first: IPv4 only, and the whole install retried
            # on a network failure (dependency resolution is unchanged).
            "for i in 1 2 3; do sudo DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 "
            "-o Acquire::http::Timeout=60 install -y /tmp/fractile.deb > /tmp/apt.log 2>&1 && break; "
            "echo \"attempt $i failed\"; sleep 30; sudo apt-get -o Acquire::ForceIPv4=true update -q > /dev/null 2>&1; done; "
            "tail -4 /tmp/apt.log; dpkg-query -W fractile", timeout=1800)
        check("V01", "apt installs the .deb on Ubuntu 24.04 desktop (dependencies from the Ubuntu archive)",
              inst.returncode == 0 and "fractile\t0.9.0" in inst.stdout, f"{rel} :: {inst.stdout.strip()[-300:]}")

        # First automatic login with the configured session type.
        vm.ssh("sudo systemctl reboot", timeout=20)
        vm.wait_down()
        vm.wait_ssh(600)
        s1 = wait_session_shell(vm, stype)
        hold("login")
        # The user starts Fractile from the app menu, then sets it up.
        vm.vmctl("launch io.fractile.Fractile.desktop")
        up = wait_for(lambda: vm.ssh("test -S /run/user/$(id -u)/fractile/fractile.sock").returncode == 0, 60)
        setup = vm.vmctl("setup")
        check("V02", f"started from the app menu in a real {session} login, Fractile is set up as the wizard does: autostart on, restore last session, launch approval for the test apps",
              bool(up) and setup.get("unit_enabled") == "enabled" and setup.get("policy") == "last_clean" and (plasma or setup.get("extension") == 0),
              f"session {s1.get('session_type')}, service socket {bool(up)}; {json.dumps(setup)[:400]}")

        # One real log-out and log-in (loads the extension on Wayland).
        shell_before = vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()
        lo = vm.vmctl("logout")
        relog = wait_for(lambda: (lambda pid: pid and pid != shell_before)(vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()), 180, 3)
        s2 = wait_session(vm, stype)
        st2 = s2.get("status") or {}
        check("V03", "after a real log-out and the next login, the systemd user unit started the service and it controls windows",
              bool(relog) and s2.get("unit_active") == "active" and bool(st2.get("window_control")),
              f"logout {lo}; new shell {bool(relog)}; unit {s2.get('unit_active')}/{s2.get('unit_enabled')}; adapter {st2.get('adapter')} window_control {st2.get('window_control')} session {s2.get('session_type')}")
        if not st2.get("window_control"):
            vm.screenshot(f"{session}-00-no-control")
            return

        lay = vm.vmctl("open-apps", timeout=900)
        before = lay.get("windows") or []
        shot = vm.screenshot(f"{session}-01-before-reboot")
        apps = sorted({w["app"] for w in before})
        if not check("V04", "10 apps / 15 windows opened and placed on a 4x4 grid",
                     len(before) == 15 and len(apps) == 10, f"{len(before)} windows, {len(apps)} apps; {shot}; {json.dumps(lay)[:300] if len(before) != 15 else ''}"):
            return

        # Real reboot.
        boot0 = s2.get("boot_id")
        vm.ssh("sudo systemctl reboot", timeout=20)
        vm.wait_down()
        vm.wait_ssh(600)
        s3 = wait_session(vm, stype)
        hold("after-reboot")
        # The default asks first: the user answers the "Restore your windows
        # from last time?" dialog with Enter (its default button).
        asked = wait_for(lambda: "decision=ask" in vm.ssh("grep 'login restore decision' ~/.local/state/fractile/fractile.log | tail -1").stdout, 120, 3)
        time.sleep(8)
        ask_shot = vm.screenshot(f"{session}-02a-restore-prompt")
        pressed = bool(asked) and vm.key("ret")
        after = wait_restore_done(vm)
        shot = vm.screenshot(f"{session}-02-after-reboot")
        ok, notes = compare(before, after.get("windows") or [])
        runs = (after.get("restore") or {}).get("recent_runs") or [{}]
        check("V05", "after a real reboot and the next login, Fractile asks first (the default); Enter on the prompt restores all 15 windows of the 10 apps where they were, with no duplicates",
              ok and s3.get("boot_id") != boot0 and runs[0].get("state") == "completed" and pressed,
              f"boot id changed {s3.get('boot_id') != boot0}; asked {bool(asked)}, Enter pressed {pressed} ({ask_shot}); last restore {runs[0].get('state')} {runs[0].get('summary')}; {'; '.join(notes)[:600]}; {shot}")

        # The user turns off "Show what will be restored and ask first".
        vm.ssh("XDG_RUNTIME_DIR=/run/user/$(id -u) fractile config set session.confirm_before_restore false", check_rc=True)

        # Real log-out and timed re-login.
        shell_before = vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()
        lo = vm.vmctl("logout")
        relog = wait_for(lambda: (lambda pid: pid and pid != shell_before)(vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()), 180, 3)
        wait_session(vm, stype)
        after2 = wait_restore_done(vm, not_run=runs[0].get("run_id"))
        shot = vm.screenshot(f"{session}-03-after-logout")
        decision = vm.ssh("grep -E 'starting pid|login session|login restore|snapshot|capture|previous session|stopping|SIGTERM|signal' ~/.local/state/fractile/fractile.log | tail -14").stdout.strip()
        ok2, notes2 = compare(before, after2.get("windows") or [])
        runs2 = (after2.get("restore") or {}).get("recent_runs") or [{}]
        check("V06", "with asking turned off, after a real log-out and the next login the same 15 windows are restored automatically, no duplicates",
              ok2 and bool(relog) and runs2[0].get("state") == "completed" and runs2[0].get("source") == "login",
              f"logout {lo.get('rc')}; new login {bool(relog)}; last restore {runs2[0].get('state')} source {runs2[0].get('source')} {runs2[0].get('summary')}; {'; '.join(notes2)[:600]}; {shot}; log: {decision[-2500:]}")
        hold("end")
        logs = vm.ssh("XDG_RUNTIME_DIR=/run/user/$(id -u) fractile logs --redacted --lines 400 2>&1 | grep -iE 'warn|error|restore' | tail -40").stdout
        (EVID / "vm").mkdir(parents=True, exist_ok=True)
        (EVID / "vm" / f"{session}-fractile-log.txt").write_text(logs)
    finally:
        vm.stop()


def wait_for(fn, timeout, step=2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return v
        time.sleep(step)
    return None


def hold(stage: str) -> None:
    """Debugging: keep the VM up at a stage; inspect with
    docker exec fractile-vm-<session> ssh -i /cache/id_ed25519 -p 2222 tester@127.0.0.1"""
    if HOLD and HOLD_AT == stage:
        say(f"holding the VM at '{stage}' for {HOLD} s (create /tmp/release in the container to go on)")
        wait_for(lambda: Path("/tmp/release").exists(), HOLD, 2)


def wait_session_shell(vm: VM, session: str, timeout=300) -> dict:
    """Logged in (shell running, right session type); Fractile not needed yet."""
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        last = vm.vmctl("session", timeout=60)
        if last.get("graphical_session") == session and last.get("shell_pid"):
            # The same shell a few seconds later: a session that is being
            # killed and restarted does not count as logged in.
            time.sleep(6)
            again = vm.vmctl("session", timeout=60)
            if again.get("shell_pid") == last.get("shell_pid") and again.get("graphical_session") == session:
                return again
            last = again
        time.sleep(4)
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", choices=sorted(SESSIONS), required=True)
    ap.add_argument("--deb", required=True)
    ap.add_argument("--provision-only", action="store_true")
    ap.add_argument("--hold", type=int, default=0, help="debug: keep the VM up this many seconds after the first login")
    ap.add_argument("--hold-at", default="login", choices=["login", "after-reboot", "end", "fail"])
    a = ap.parse_args()
    global HOLD, HOLD_AT
    HOLD = min(a.hold, 2400)
    HOLD_AT = a.hold_at
    if a.provision_only:
        ensure_plasma_base() if a.session == "plasma-x11" else ensure_base()
        return 0
    try:
        run(a.session, a.deb)
    except Exception as ex:  # noqa: BLE001  record, then still write evidence
        import traceback
        traceback.print_exc()
        check("VXX", "run crashed", False, repr(ex))
    ev = {"suite": f"vm-ubuntu24.04-{a.session}", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"Ubuntu 24.04 desktop VM (QEMU/KVM, 4 vCPU, 6 GiB, virtio-vga 1600x1200, software rendering){' with kde-plasma-desktop (Plasma 5.27)' if a.session == 'plasma-x11' else ''}, GDM timed login, {a.session} session, Fractile {Path(a.deb).name} installed with apt",
          "results": results, "log": log_lines}
    out = EVID / f"vm-ubuntu24.04-{a.session}.json"
    out.write_text(json.dumps(ev, indent=2) + "\n")
    print(f"{sum(r['ok'] for r in results)}/{len(results)} checks passed; evidence: {out}")
    return 0 if results and all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
