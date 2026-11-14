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
import shlex
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
# Ubuntu release -> version. Plasma 6 needs 26.04: 24.04 ships Plasma 5.27.
RELEASES = {"noble": "24.04", "resolute": "26.04"}
SSH_PORT = 2222
QMP = "/tmp/qmp.sock"
GUEST_TOOL = "/src/tests/vm/guest/vmctl.py"
# session -> (GDM session name, logind session type, the shell process)
SESSIONS = {"wayland": ("ubuntu", "wayland", "gnome-shell"), "x11": ("ubuntu-xorg", "x11", "gnome-shell"),
            "plasma-x11": ("plasma", "x11", "plasmashell"), "plasma-wayland": ("plasma", "wayland", "plasmashell")}
# The Ubuntu release each session runs on.
SESSION_RELEASE = {"wayland": "noble", "x11": "noble", "plasma-x11": "noble", "plasma-wayland": "resolute"}
PLASMA_VERSIONS = "dpkg-query -W -f='${Package} ${Version}; ' plasma-workspace kwin-wayland kwin-x11 2>/dev/null"
APT = ("sudo DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=60")
APPS_PKGS = ("ubuntu-desktop-minimal linux-generic gnome-terminal gnome-text-editor gnome-calculator "
             "gnome-system-monitor gnome-characters gnome-clocks baobab gnome-disk-utility gnome-font-viewer")

HOLD = 0
HOLD_AT = "login"
POWER_CUT = False
PANEL = False  # --panel: a USB tablet for absolute pointer input
DB = "~/.local/share/fractile/fractile.db"
CLI_ENV = "XDG_RUNTIME_DIR=/run/user/$(id -u)"
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

def ensure_cloud_image(release: str = "noble") -> Path:
    name = f"{release}-server-cloudimg-amd64.img"
    img = CACHE / name
    if img.exists():
        return img
    IMG_URL = f"https://cloud-images.ubuntu.com/{release}/current/{name}"
    SUMS_URL = f"https://cloud-images.ubuntu.com/{release}/current/SHA256SUMS"
    sums = urllib.request.urlopen(SUMS_URL, timeout=60).read().decode()
    want = next(line.split()[0] for line in sums.splitlines() if line.endswith(name))
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
    if PANEL:
        # A tablet reports absolute positions, so the harness can put the
        # pointer on an exact pixel (a mouse only moves relatively).
        cmd += ["-device", "qemu-xhci", "-device", "usb-tablet"]
    return cmd


def ensure_base(release: str = "noble") -> Path:
    # 24.04 keeps its original cache names.
    sfx = "" if release == "noble" else f"-{release}"
    base = CACHE / f"base-desktop{sfx}.qcow2"
    if base.exists():
        return base
    img = ensure_cloud_image(release)
    tmp = CACHE / f"base-desktop{sfx}.part.qcow2"
    subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(img), str(tmp)], check=True)
    subprocess.run(["qemu-img", "resize", str(tmp), "24G"], check=True)
    sd = seed(Path("/tmp/seed.img"))
    say(f"provisioning the base image: Ubuntu {RELEASES[release]} desktop and the test apps (cloud-init)")
    serial = CACHE / f"provision{sfx}-serial.log"
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


def ensure_plasma_base(release: str = "noble") -> Path:
    """The desktop base plus kde-plasma-desktop, GDM kept as the display
    manager, and the Plasma screen locker off (as the GNOME base turns its
    own off): an idle lock would make Fractile skip its captures."""
    sfx = "" if release == "noble" else f"-{release}"
    out = CACHE / f"base-plasma{sfx}.qcow2"
    if out.exists():
        return out
    base = ensure_base(release)
    # Plasma 6 (26.04) is tested on Wayland, Plasma 5.27 (24.04) on X11.
    sess_file = "/usr/share/xsessions/plasma.desktop" if release == "noble" else "/usr/share/wayland-sessions/plasma.desktop"
    say("provisioning the Plasma image: kde-plasma-desktop on the desktop base")
    vm = VM(base, "plasma-provision")
    try:
        if not vm.wait_ssh(600):
            raise SystemExit("the Plasma provisioning VM did not come up")
        steps = [
            "echo 'gdm3 shared/default-x-display-manager select gdm3' | sudo debconf-set-selections",
            f"for i in 1 2 3; do {APT} update -q && {APT} install -y kde-plasma-desktop > /tmp/plasma.log 2>&1 && break; sleep 30; done; tail -3 /tmp/plasma.log",
            f"test -e {sess_file}",
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

    def qmp(self, command: str, arguments: dict | None = None) -> dict:
        s = socket.socket(socket.AF_UNIX)
        s.connect(QMP)
        f = s.makefile("rw")
        f.readline()
        r: dict = {}
        msgs = [{"execute": "qmp_capabilities"}, {"execute": command, **({"arguments": arguments} if arguments else {})}]
        for msg in msgs:
            f.write(json.dumps(msg) + "\n")
            f.flush()
            while True:
                r = json.loads(f.readline())
                if "return" in r or "error" in r:
                    break
        s.close()
        return r

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

    # -- real input for the panel tests: QEMU's virtual keyboard and USB
    # tablet, the way a user's devices reach the desktop. Fractile itself
    # injects nothing.
    def events(self, *evs: dict) -> bool:
        return "return" in self.qmp("input-send-event", {"events": list(evs)})

    def keydown(self, k: str, down: bool = True) -> bool:
        return self.events({"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": k}}})

    def combo(self, combo: str) -> bool:
        keys = combo.split("+")
        ok = all(self.keydown(k) for k in keys)
        time.sleep(0.05)
        ok = all(self.keydown(k, False) for k in reversed(keys)) and ok
        time.sleep(0.25)
        return ok

    def pointer_to(self, x: float, y: float, screen: tuple[int, int]) -> bool:
        """Absolute position in the desktop's coordinates (one output)."""
        w, h = screen
        return self.events({"type": "abs", "data": {"axis": "x", "value": round(x * 32767 / max(1, w - 1))}},
                           {"type": "abs", "data": {"axis": "y", "value": round(y * 32767 / max(1, h - 1))}})

    def button(self, down: bool) -> bool:
        return self.events({"type": "btn", "data": {"down": down, "button": "left"}})

    def call(self, method: str, params: dict | None = None) -> dict:
        return self.vmctl(f"call {method} {shlex.quote(json.dumps(params or {}))}")

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


def base_for(session: str) -> Path:
    rel = SESSION_RELEASE[session]
    return ensure_plasma_base(rel) if session.startswith("plasma") else ensure_base(rel)


def run(session: str, deb: str) -> None:
    base = base_for(session)
    _xs, stype, shell = SESSIONS[session]
    plasma = session.startswith("plasma")
    ver = RELEASES[SESSION_RELEASE[session]]
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
        check("V01", f"apt installs the .deb on Ubuntu {ver} desktop (dependencies from the Ubuntu archive)",
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
              bool(up) and setup.get("unit_enabled") == "enabled" and setup.get("policy") == "last_clean" and setup.get("extension") == ("n/a" if session == "plasma-x11" else 0),
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

        if POWER_CUT:
            power_cut(vm, session, stype, before)
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
        asked = wait_for(lambda: "decision=ask" in vm.ssh("grep -a 'login restore decision' ~/.local/state/fractile/fractile.log | tail -1").stdout, 120, 3)
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
        decision = vm.ssh("grep -a -E 'starting pid|login session|login restore|snapshot|capture|previous session|stopping|SIGTERM|signal' ~/.local/state/fractile/fractile.log | tail -14").stdout.strip()
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


def run_panel(session: str, deb: str) -> None:
    """The panel in a real desktop login, driven with the VM's keyboard and
    tablet (DV-01, DV-02, DV-04, DV-05, DV-07, DV-09, DV-10, DV-11, DV-15,
    DV-21, DV-24, DV-27): the same interactions as tests/gui/gui_x11.py and
    tests/gui/panel_gnome_wayland.py."""
    base = base_for(session)
    _xs, stype, shell = SESSIONS[session]
    plasma = session.startswith("plasma")
    ver = RELEASES[SESSION_RELEASE[session]]
    vm = VM(base, f"panel-{session}")
    try:
        if not check("P00", "the VM boots and answers over ssh", vm.wait_ssh(600)):
            return
        gdm_and_session(vm, session)
        vm.scp(deb, "/tmp/fractile.deb")
        vm.scp(GUEST_TOOL, "/home/tester/vmctl.py")
        rel = vm.ssh(f". /etc/os-release; echo $PRETTY_NAME; {PLASMA_VERSIONS if plasma else 'gnome-shell --version'}").stdout.strip().replace("\n", "; ")
        inst = vm.ssh("for i in 1 2 3; do sudo DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 "
                      "-o Acquire::http::Timeout=60 install -y /tmp/fractile.deb python3-evdev x11-utils > /tmp/apt.log 2>&1 && break; sleep 30; "
                      "sudo apt-get -o Acquire::ForceIPv4=true update -q > /dev/null 2>&1; done; dpkg-query -W fractile", timeout=1800)
        if not check("P01", f"the .deb installs ({rel})", inst.returncode == 0 and "fractile\t0.9.0" in inst.stdout, inst.stdout.strip()[-200:]):
            return
        vm.ssh("sudo systemctl reboot", timeout=20)
        vm.wait_down()
        vm.wait_ssh(600)
        wait_session_shell(vm, stype)
        vm.vmctl("launch io.fractile.Fractile.desktop")
        wait_for(lambda: vm.ssh("test -S /run/user/$(id -u)/fractile/fractile.sock").returncode == 0, 60)
        setup = vm.vmctl("setup")
        # a log-out and log-in loads the GNOME extension (and is harmless on Plasma)
        shell_before = vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()
        vm.vmctl("logout")
        wait_for(lambda: (lambda pid: pid and pid != shell_before)(vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()), 180, 3)
        s2 = wait_session(vm, stype)
        prep = vm.vmctl("panel-prep")
        st = prep.get("status") or {}
        if not check("P02", f"real {session} login: the service runs from the systemd unit and controls windows",
                     bool(st.get("window_control")) and s2.get("unit_active") == "active",
                     f"adapter {st.get('adapter')} session {st.get('session_type')}; setup {json.dumps(setup)[:200]}; prep {json.dumps(prep)[:300]}"):
            vm.screenshot(f"panel-{session}-00-no-control")
            return

        # DV-01: one instance
        pid0 = (vm.call("status").get("result") or {}).get("pid")
        second = vm.ssh("XDG_RUNTIME_DIR=/run/user/$(id -u) timeout 10 fractiled 2>&1; echo rc=$?")
        pid1 = (vm.call("status").get("result") or {}).get("pid")
        check("P03", "a second service started by hand says it is already running and leaves the running one alone (one instance)",
              "already running" in second.stdout and "rc=124" not in second.stdout and pid0 and pid0 == pid1,
              f"{second.stdout.strip()[-200:]}; service pid {pid0} -> {pid1}")

        outs = (vm.call("list_outputs").get("result") or [])
        out = outs[0]
        screen = (out["rect"]["w"], out["rect"]["h"])
        vm.vmctl("launch org.gnome.TextEditor.desktop")
        ed = wait_for(lambda: next((w for w in (vm.call("list_windows").get("result") or []) if w.get("desktop_file_id") == "org.gnome.TextEditor.desktop"), None), 60, 2)
        if not check("P04", "the target app (Text Editor) is up and placeable", bool(ed), f"outputs {[(o['stable_id'], o['rect']) for o in outs]}"):
            return
        wid = ed["id"]

        def frame(w=wid):
            return next((x["frame"] for x in (vm.call("list_windows", {"all": True}).get("result") or []) if x["id"] == w), None)

        def rect(cols, rows, c0, r0, c1, r1):
            return vm.call("compute_rect", {"grid": {"cols": cols, "rows": rows}, "selection": {"col0": c0, "row0": r0, "col1": c1, "row1": r1}}).get("result")

        def near(a, b, tol=2):
            return bool(a and b) and all(abs(a[k] - b[k]) <= tol for k in ("x", "y", "w", "h"))

        def placed(want, t=6):
            return wait_for(lambda: (lambda f: f if near(f, want) else None)(frame()), t, 0.5) or frame()

        def active():
            return vm.call("active_window").get("result")

        def panel_open():
            return bool(vm.vmctl("panel-geom").get("grids")) and any((w.get("title") or "").startswith("Fractile panel") and not w["state"].get("minimized")
                                                                    for w in (vm.call("list_windows", {"all": True}).get("result") or []))

        def open_panel(w=wid):
            vm.ssh("rm -f /run/user/$(id -u)/fractile-panel-geometry.json")
            vm.call("activate_window", {"window": w})
            time.sleep(0.8)
            vm.combo("ctrl+alt+g")
            g = wait_for(lambda: (vm.vmctl("panel-geom").get("grids") or [None])[0], 15, 0.5)
            if g:
                time.sleep(1.0)  # let the compositor finish placing a new panel window
                g = (vm.vmctl("panel-geom").get("grids") or [g])[0]
            return g

        def close_if_open():
            if panel_open():
                vm.combo("esc")
                wait_for(lambda: not panel_open(), 5, 0.5)

        def cell(g, col, row):
            cw, ch = g["w"] / g["cols"], g["h"] / g["rows"]
            return g["x"] + (col + 0.5) * cw, g["y"] + (row + 0.5) * ch

        def drag(g, a, b, hold=None, shot=None):
            ax, ay = cell(g, *a)
            bx, by = cell(g, *b)
            vm.pointer_to(ax, ay, screen)
            time.sleep(0.3)
            if hold:
                vm.keydown(hold)
                time.sleep(0.4)
            vm.button(True)
            time.sleep(0.2)
            vm.pointer_to((ax + bx) / 2, (ay + by) / 2, screen)
            time.sleep(0.2)
            vm.pointer_to(bx, by, screen)
            time.sleep(0.8)
            mid_frame = frame()
            drag.active = active()
            if session.endswith("x11"):
                # On X11 the preview is an override-redirect window, which no
                # window list carries: ask the X server.
                xp = vm.vmctl("x11-preview")
                prev = [{"frame": xp["frame"]}] if xp.get("frame") else []
            else:
                prev = [x for x in (vm.call("list_windows", {"all": True}).get("result") or []) if (x.get("title") or "").startswith("Fractile preview")]
            s = vm.screenshot(shot) if shot else None
            vm.button(False)
            time.sleep(0.2)
            if hold:
                vm.keydown(hold, False)
            return mid_frame, prev, s

        vm.call("snap", {"action": "center", "window": wid})
        g = open_panel()
        if not check("P05", "the panel shortcut opens the panel for the focused window", bool(g), json.dumps(vm.vmctl("panel-geom"))[:200]):
            vm.screenshot(f"panel-{session}-05-no-panel")
            return
        vm.screenshot(f"panel-{session}-05-panel")

        # DV-04 drag, DV-21 preview, DV-10 dismiss after applying
        want = rect(g["cols"], g["rows"], 0, 0, 2, g["rows"] - 1)
        mid, prev, s = drag(g, (0, 0), (2, g["rows"] - 1), shot=f"panel-{session}-06-drag-preview")
        got = placed(want)
        check("P06", "dragging across the grid places the window exactly", near(got, want), f"want {want} got {got}")
        if plasma:
            pv = prev[0]["frame"] if prev else None
            pan = g.get("panel_id")
            check("P07", "during the drag a preview window shows the exact target, the panel keeps keyboard focus, and the window has not moved yet",
                  near(pv, want, 3) and not near(mid, want) and drag.active == pan, f"preview {pv} want {want}; active {drag.active} (panel {pan}); window then {mid}; {s}")
        gone = wait_for(lambda: not panel_open(), 5, 0.5)
        check("P08", "the panel closes after placing and focus returns to the window", bool(gone) and wait_for(lambda: active() == wid, 5, 0.5), f"active {active()}")

        # DV-09 Esc
        before = frame()
        open_panel()
        vm.combo("right")
        vm.combo("esc")
        gone = wait_for(lambda: not panel_open(), 5, 0.5)
        time.sleep(0.5)
        check("P09", "Esc closes the panel without moving the window and focus returns to it", bool(gone) and near(frame(), before, 0) and wait_for(lambda: active() == wid, 5, 0.5),
              f"{before} -> {frame()}; active {active()}")

        # DV-07, DV-24 keep
        g1 = open_panel()
        vm.combo("alt+equal")
        time.sleep(0.4)
        vm.combo("alt+shift+equal")
        time.sleep(0.4)
        vm.combo("ctrl+alt+g")
        wait_for(lambda: not panel_open(), 5, 0.5)
        g2 = open_panel()
        check("P10", "Alt+= / Alt+Shift+= add a column / row, and the size is kept the next time the panel opens",
              bool(g1 and g2) and (g2["cols"], g2["rows"]) == (g1["cols"] + 1, g1["rows"] + 1), f"{g1 and (g1['cols'], g1['rows'])} -> {g2 and (g2['cols'], g2['rows'])}")
        vm.combo("alt+minus")
        time.sleep(0.3)
        vm.combo("alt+shift+minus")
        time.sleep(0.3)
        close_if_open()

        # DV-11 Shift+drag keeps it open
        g = open_panel()
        if g:
            want = rect(g["cols"], g["rows"], g["cols"] - 2, 0, g["cols"] - 1, 1)
            drag(g, (g["cols"] - 2, 0), (g["cols"] - 1, 1), hold="shift")
            got = placed(want)
            still = panel_open()
            check("P11", "Shift+drag places the window and keeps the panel open", near(got, want) and still, f"want {want} got {got} open {still}")
        close_if_open()

        # DV-05 hold Alt
        vm.call("set_config", {"config": dict(vm.call("get_config")["result"]["config"], appearance=dict(vm.call("get_config")["result"]["config"]["appearance"], default_grid={"cols": 10, "rows": 10}))})
        time.sleep(1)
        g = open_panel()
        if g and (g["cols"], g["rows"]) == (10, 10):
            drag(dict(g, cols=20, rows=20), (1, 1), (12, 14), hold="alt", shot=f"panel-{session}-12-subdivided")
            want = rect(20, 20, 1, 1, 12, 14)
            got = placed(want)
            check("P12", "holding Alt subdivides 10x10 to 20x20 and a half-cell selection lands exactly", near(got, want), f"want {want} got {got}")
        else:
            check("P12", "holding Alt subdivides 10x10 to 20x20 and a half-cell selection lands exactly", False, f"grid {g and (g['cols'], g['rows'])}")
        close_if_open()
        cfg = vm.call("get_config")["result"]["config"]
        cfg["appearance"]["default_grid"] = {"cols": 6, "rows": 6}
        vm.call("set_config", {"config": cfg})
        time.sleep(1)

        # DV-15 local shortcut only while open
        vm.call("snap", {"action": "center", "window": wid})
        open_panel()
        vm.combo("ctrl+left")
        want = rect(2, 1, 0, 0, 0, 0)
        got = placed(want)
        closed = wait_for(lambda: not panel_open(), 5, 0.5)
        vm.call("snap", {"action": "center", "window": wid})
        centered = frame()
        vm.call("activate_window", {"window": wid})
        time.sleep(0.8)
        vm.combo("ctrl+left")
        time.sleep(1.5)
        check("P13", "panel-local shortcut (Ctrl+Left) snaps while the panel is open and does nothing once it is closed",
              near(got, want) and bool(closed) and near(frame(), centered, 0), f"in panel {got}; after close {frame()} (centered {centered})")

        # DV-24 reset on open
        cfg = vm.call("get_config")["result"]["config"]
        cfg["appearance"]["reset_grid_on_open"] = True
        vm.call("set_config", {"config": cfg})
        time.sleep(1)
        g1 = open_panel()
        vm.combo("alt+equal")
        time.sleep(0.5)
        close_if_open()
        g2 = open_panel()
        check("P14", "with reset on open, a grid changed with Alt+= is back to the default 6x6 next time",
              bool(g1 and g2) and (g1["cols"], g2["cols"], g2["rows"]) == (6, 6, 6), f"{g1 and (g1['cols'], g1['rows'])} -> {g2 and (g2['cols'], g2['rows'])}")
        close_if_open()
        cfg["appearance"]["reset_grid_on_open"] = False
        vm.call("set_config", {"config": cfg})

        # DV-27: the target closes while the panel is open
        vm.vmctl("launch org.gnome.Calculator.desktop")
        calc = wait_for(lambda: next((w for w in (vm.call("list_windows").get("result") or []) if w.get("desktop_file_id") == "org.gnome.Calculator.desktop"), None), 60, 2)
        if calc:
            before_ed = frame()
            g = open_panel(calc["id"])
            vm.ssh("pkill -u tester -f gnome-calculator")
            gone = wait_for(lambda: not any(w["id"] == calc["id"] for w in (vm.call("list_windows").get("result") or [])), 15, 0.5)
            time.sleep(1)
            shot = vm.screenshot(f"panel-{session}-15-target-closed")
            if g:
                drag(g, (0, 0), (2, 2))
                time.sleep(1)
            still = panel_open()
            check("P15", "when the target closes with the panel open, the panel stays (with a notice), a drag places nothing and no other window moves",
                  bool(g) and bool(gone) and still and near(frame(), before_ed, 0), f"panel {bool(g)} gone {bool(gone)} open {still}; editor {before_ed} -> {frame()}; {shot}")
            close_if_open()
        else:
            check("P15", "when the target closes with the panel open, the panel stays (with a notice), a drag places nothing and no other window moves", False, "Calculator did not open")

        # DV-02: launching the app again (app menu) shows the panel for the focused window
        vm.call("activate_window", {"window": wid})
        time.sleep(0.8)
        vm.ssh("rm -f /run/user/$(id -u)/fractile-panel-geometry.json")
        vm.vmctl("launch io.fractile.Fractile.desktop")
        g = wait_for(lambda: (vm.vmctl("panel-geom").get("grids") or [None])[0], 15, 0.5)
        tgt = (vm.call("panel_state").get("result") or {}).get("target") or {}
        check("P16", "starting Fractile again from the app menu opens the panel for the focused window", bool(g) and tgt.get("id") == wid, f"panel {bool(g)} target {tgt.get('id')} (editor {wid})")
        close_if_open()

        log = vm.ssh("grep -c Traceback ~/.local/state/fractile/ui.log 2>/dev/null; journalctl --user -q --no-pager | grep -c 'Traceback' ").stdout.split()
        check("P17", "no Python errors in the UI's output", all(x == "0" for x in log), f"{log}")

        # DV-19 / DV-02: the tray icon in the desktop's own tray
        if plasma:
            vm.ssh("XDG_RUNTIME_DIR=/run/user/$(id -u) fractile config set general.tray_icon true")
            tr = vm.vmctl("tray")
            shot = vm.screenshot(f"panel-{session}-18-tray")
            check("P18", "with the tray icon on, Fractile's icon is registered in the desktop's tray (StatusNotifierWatcher) and active",
                  tr.get("registered") and "Active" in (tr.get("status") or ""), f"{json.dumps(tr)[:300]}; {shot}")
            vm.call("activate_window", {"window": wid})
            time.sleep(0.8)
            vm.ssh("rm -f /run/user/$(id -u)/fractile-panel-geometry.json")
            act = vm.vmctl("tray-activate")
            g = wait_for(lambda: (vm.vmctl("panel-geom").get("grids") or [None])[0], 15, 0.5)
            tgt = (vm.call("panel_state").get("result") or {}).get("target") or {}
            check("P19", "a left click on the tray icon (StatusNotifierItem.Activate, what the tray sends) shows the panel for the focused window",
                  bool(g) and tgt.get("id") == wid, f"{act}; panel {bool(g)} target {tgt.get('id')} (editor {wid})")
            close_if_open()

            # DV-14, DV-25, DV-26: the Settings window, keys from a uinput keyboard
            vm.ssh("mkdir -p ~/t/tests/gui ~/t/tests/tools; XDG_RUNTIME_DIR=/run/user/$(id -u) fractile config set general.tray_icon false")
            # the probe is the UI now (the pattern must not match this ssh shell's own command line)
            vm.ssh("pkill -u tester -f '^/usr/bin/python3 .*/fractile-[u]i'; pkill -u tester -f '^python3 .*/fractile-[u]i'; true")
            vm.scp("/src/tests/gui/settings_probe.py", "/home/tester/t/tests/gui/settings_probe.py")
            vm.scp("/src/tests/tools/uinput_keys.py", "/home/tester/t/tests/tools/uinput_keys.py")
            out = vm.vmctl("settings-probe", timeout=900)
            if not out.get("results"):
                check("P-SXX", "the Settings probe produced results", False, json.dumps(out)[:1500])
            for r in out.get("results") or []:
                check(f"P-{r['id']}", r["name"], r["ok"], r.get("detail", ""))
            vm.screenshot(f"panel-{session}-20-after-settings")
            vm.call("activate_window", {"window": wid})
            time.sleep(0.8)
            vm.combo("ctrl+alt+shift+f9")
            want = rect(2, 1, 0, 0, 0, 0)
            got = placed(want)
            check("P-S11", "pressing the shortcut created in Settings places the window (left half)", near(got, want), f"want {want} got {got}")
        hold("end")
    finally:
        vm.stop()


# After a power cut the service log can end in a block of NUL bytes (data
# the kernel had not written yet), so every grep on it uses -a.


def db_check(vm: VM) -> dict:
    """SQLite's own integrity check on the store, and every snapshot row's
    payload read back through Fractile (checksum verified)."""
    py = ("import sqlite3,os,json;p=os.path.expanduser('" + DB + "');c=sqlite3.connect('file:'+p+'?mode=ro',uri=True);"
          "print(json.dumps({'integrity':c.execute('pragma integrity_check').fetchone()[0],"
          "'rows':c.execute('select count(*) from snapshots').fetchone()[0]}))")
    r = vm.ssh(f"python3 -c \"{py}\"")
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": (r.stdout + r.stderr)[-400:]}
    lst = vm.ssh(f"{CLI_ENV} fractile --json list-snapshots --limit 1000").stdout
    try:
        snaps = json.loads(lst)
    except ValueError:
        snaps = []
    bad = 0
    for sn in snaps:
        sid = sn.get("snapshot_id") or sn.get("id")
        if vm.ssh(f"{CLI_ENV} fractile --json snapshot export {sid} /dev/null").returncode != 0:
            bad += 1
    out.update({"listed": len(snaps), "unreadable": bad})
    return out


def cut_power_while_writing(vm: VM, tag: str) -> dict:
    """Snapshots written back to back (manual saves, each a SQLite
    transaction with synchronous=FULL), then the VM is reset with no
    shutdown: the guest kernel, every process and the guest's page cache
    are gone at once, as at a power cut."""
    vm.ssh(f"setsid nohup bash -c 'i=0; while true; do {CLI_ENV} fractile save --name {tag}-$i >/dev/null 2>&1; i=$((i+1)); echo $i > /tmp/saves; done' "
           "> /dev/null 2>&1 < /dev/null &", timeout=20)
    time.sleep(12)
    n = vm.ssh("cat /tmp/saves").stdout.strip()
    boot = vm.ssh("cat /proc/sys/kernel/random/boot_id").stdout.strip()
    r = vm.qmp("system_reset")
    say(f"power cut ({tag}) after {n} snapshot saves: {r}")
    vm.wait_down(60)
    vm.wait_ssh(600)
    return {"saves_before_cut": n, "boot_before": boot, "qmp": "return" in r}


def power_cut(vm: VM, session: str, stype: str, before: list[dict]) -> None:
    """RS-18 / T15. Two power cuts during snapshot writes:
      1. defaults (restore last clean session, ask on power loss): after the
         cut the store is intact, the session is marked unclean, the user is
         asked ("ended unexpectedly"), and Enter restores the layout.
      2. "restore last known session" with power-loss policy "restore", and
         nothing moved since the restore: restored without asking."""
    env = CLI_ENV
    vm.ssh(f"{env} fractile config set session.autosave_interval_min 1", check_rc=True)
    wait_for(lambda: vm.ssh("grep -a -q ' autosave ' ~/.local/state/fractile/fractile.log").returncode == 0, 200, 5)
    al = vm.ssh("grep -a ' autosave ' ~/.local/state/fractile/fractile.log | tail -1").stdout.strip()
    check("PC00", "with a 1 min autosave interval, a periodic snapshot of the placed layout is written", bool(al), al[-200:])

    c1 = cut_power_while_writing(vm, "pc1")
    s = wait_session(vm, stype)
    dbc = db_check(vm)
    check("PC01", "after a power cut in the middle of back-to-back snapshot writes, the store passes SQLite's integrity check and every snapshot listed reads back with a valid checksum",
          c1["qmp"] and s.get("boot_id") != c1["boot_before"] and dbc.get("integrity") == "ok" and dbc.get("unreadable") == 0 and dbc.get("listed", 0) > 0,
          f"{c1}; new boot {s.get('boot_id') != c1['boot_before']}; {dbc}")
    asked = wait_for(lambda: "decision=ask unclean=true" in vm.ssh("grep -a 'login restore decision' ~/.local/state/fractile/fractile.log | tail -1").stdout, 180, 3)
    dec = vm.ssh("grep -a 'login restore decision' ~/.local/state/fractile/fractile.log | tail -1").stdout.strip()
    time.sleep(8)
    shot = vm.screenshot(f"{session}-pc1-prompt")
    pressed = bool(asked) and vm.key("ret")
    after = wait_restore_done(vm)
    ok, notes = compare(before, after.get("windows") or [])
    runs = (after.get("restore") or {}).get("recent_runs") or [{}]
    check("PC02", "the next login treats the session as ended unexpectedly: with the default power-loss policy it asks first, and Enter restores all 15 windows from the last periodic snapshot",
          bool(asked) and pressed and ok and runs[0].get("state") == "completed",
          f"{dec[-200:]}; Enter {pressed}; {shot}; last restore {runs[0].get('state')} {runs[0].get('summary')}; {'; '.join(notes)[:500]}")

    vm.ssh(f"{env} fractile config set session.policy last_known && {env} fractile config set session.power_loss_policy restore", check_rc=True)
    # Longer than the autosave interval, with nothing moved since the restore.
    time.sleep(75)
    run1 = runs[0].get("run_id")
    c2 = cut_power_while_writing(vm, "pc2")
    s = wait_session(vm, stype)
    dbc2 = db_check(vm)
    decided = wait_for(lambda: (lambda l: l if "login restore decision" in l and l != dec else None)(
        vm.ssh("grep -a -E 'login restore decision|no snapshot from the previous session' ~/.local/state/fractile/fractile.log | tail -1").stdout.strip()), 180, 3)
    after2 = wait_restore_done(vm, not_run=run1)
    ok2, notes2 = compare(before, after2.get("windows") or [])
    runs2 = (after2.get("restore") or {}).get("recent_runs") or [{}]
    shot2 = vm.screenshot(f"{session}-pc2-after")
    check("PC03", "a second power cut, with nothing moved since the last restore: the store is intact, and with \"last known session\" and power-loss policy \"restore\" all 15 windows come back without a prompt",
          dbc2.get("integrity") == "ok" and dbc2.get("unreadable") == 0 and bool(decided) and "decision=restore unclean=true" in (decided or "") and ok2
          and runs2[0].get("run_id") != run1 and runs2[0].get("state") == "completed" and runs2[0].get("source") == "login",
          f"{c2}; {dbc2}; {decided}; last restore {runs2[0].get('state')} source {runs2[0].get('source')} {runs2[0].get('summary')}; {'; '.join(notes2)[:500]}; {shot2}")
    logs = vm.ssh("grep -a -iE 'warn|error|restore|unclean|autosave|session' ~/.local/state/fractile/fractile.log | tail -60").stdout
    (EVID / "vm" / f"{session}-power-cut-log.txt").write_text(logs)


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


SOAK_PKGS = ("xvfb x11-utils x11-xserver-utils xdotool dbus-x11 python3-xlib python3-gi gir1.2-gtk-4.0 "
             "python3-pyqt6 libglib2.0-bin sqlite3")


def soak(minutes: float) -> int:
    """T27: tests/perf/soak_x11.py inside the VM, so a long run uses the VM's
    memory and never the user's desktop. Runs on Xvfb with the guest's GNOME
    Shell (X11) as the window manager, under the soak's own memory cap, as a
    transient systemd user service (linger on, so it does not depend on the
    ssh session). Progress goes to stdout and tests/evidence/vm/soak-progress.log."""
    base = ensure_base()
    vm = VM(base, "soak")
    # The 24 h run keeps its file names; a run of another length gets its own,
    # so it never overwrites the 24 h evidence.
    tag = "" if minutes == 1440 else f"-{minutes:g}min"
    prog = EVID / "vm" / f"soak{tag}-progress.log"
    prog.parent.mkdir(exist_ok=True)
    def note(msg: str) -> None:
        say(msg)
        with prog.open("a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + msg + "\n")
    prog.write_text("")
    ok = False
    try:
        if not vm.wait_ssh(600):
            note("the VM did not answer over ssh")
            return 1
        rel = vm.ssh(". /etc/os-release; echo $PRETTY_NAME; gnome-shell --version; uname -r; nproc; free -m | awk '/Mem/{print $2\" MiB\"}'").stdout.strip().replace("\n", "; ")
        note(f"guest: {rel}")
        inst = vm.ssh(f"for i in 1 2 3; do {APT} update -q >/dev/null 2>&1; {APT} install -y --no-install-recommends {SOAK_PKGS} > /tmp/apt.log 2>&1 && break; "
                      "sleep 30; done; tail -1 /tmp/apt.log; sudo loginctl enable-linger tester; dpkg-query -W python3-pyqt6 xvfb", timeout=2400)
        note(f"soak dependencies: {inst.stdout.strip().splitlines()[-2:]}")
        tar = subprocess.Popen(["tar", "-C", "/src", "-cf", "-", "--exclude=__pycache__", "--exclude=tests/evidence", "tests", "ui",
                                "target/release/fractiled", "target/release/fractile"], stdout=subprocess.PIPE)
        put = subprocess.run(["ssh", "-q", "-i", str(vm.ssh_key), "-p", str(SSH_PORT), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                              "-o", "BatchMode=yes", "tester@127.0.0.1", "mkdir -p fractile/tests/evidence && tar -C fractile -xf -"], stdin=tar.stdout, timeout=600)
        tar.wait()
        if put.returncode != 0 or tar.returncode != 0:
            note(f"copying the build into the VM failed (tar {tar.returncode}, ssh {put.returncode})")
            return 1
        cmd = (f"cd ~/fractile && exec python3 tests/perf/soak_x11.py --display 31 --minutes {minutes} --memory-max 3G "
               f"--evidence /home/tester/soak.json --keep-log /home/tester/soak-daemon.log > /home/tester/soak.log 2>&1")
        st = vm.ssh("systemd-run --user --quiet --unit=fractile-soak -E FRACTILE_BIN_DIR=/home/tester/fractile/target/release "
                    f"-E FRACTILE_TEST_XSERVER=xvfb -E PYTHONDONTWRITEBYTECODE=1 bash -c '{cmd}'; sleep 5; systemctl --user is-active fractile-soak")
        note(f"soak started ({minutes} min): {st.stdout.strip()} {st.stderr.strip()[-300:]}")
        end = time.time() + minutes * 60 + 3 * 3600
        last = ""
        while time.time() < end:
            time.sleep(300)
            try:
                r = vm.ssh("systemctl --user is-active fractile-soak; grep -E '^(soak progress|PASS|FAIL|soak error)' soak.log | tail -1; "
                           "free -m | awk '/Mem/{print \"guest used \"$3\" of \"$2\" MiB\"}'", timeout=60)
            except subprocess.TimeoutExpired:
                note("the VM did not answer this poll")
                continue
            lines = r.stdout.strip().splitlines()
            state = lines[0] if lines else "?"
            body = " | ".join(lines[1:])
            if body != last:
                note(f"[{state}] {body}")
                last = body
            if state not in ("active", "activating"):
                break
        for src, dst in (("soak.json", EVID / f"perf-x11-{'24h' if not tag else tag[1:]}.json"), ("soak.log", EVID / "vm" / f"soak{tag}.log")):
            r = vm.ssh(f"cat {src}", timeout=120)
            if r.returncode == 0 and r.stdout:
                dst.write_text(r.stdout)
        r = vm.ssh("tail -3000 soak-daemon.log", timeout=120)
        (EVID / "vm" / f"soak{tag}-daemon-tail.log").write_text(r.stdout)
        res = vm.ssh("tail -2 soak.log").stdout.strip()
        note(f"soak finished: {res}")
        ok = "checks passed" in res and res.split("/")[0].split()[-1] == res.split("/")[1].split()[0]
        if not ok:
            # Diagnose in place: the test desktop and test windows by hand.
            d = vm.ssh("cd ~/fractile; export XDG_RUNTIME_DIR=/run/user/$(id -u); "
                       "FRACTILE_TEST_XSERVER=xvfb bash tests/integration/nested-gnome-x11.sh start 33 1600x1200 2>&1 | tail -3; "
                       "for tk in gtk4 qt; do DISPLAY=:33 timeout 8 python3 tests/tools/testwin.py --toolkit $tk --app-id org.diag.$tk --title diag-$tk 2>&1 | tail -5 & done; "
                       "sleep 5; DISPLAY=:33 xprop -root _NET_CLIENT_LIST; wait; "
                       "tail -15 /run/user/$(id -u)/fractile-nested-33/shell.log; bash tests/integration/nested-gnome-x11.sh stop 33", timeout=180)
            note("diagnostics:\n" + d.stdout[-4000:] + d.stderr[-1000:])
            hold("fail")
    finally:
        vm.stop()
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", choices=sorted(SESSIONS))
    ap.add_argument("--deb")
    ap.add_argument("--soak-minutes", type=float, default=0, help="run the T27 soak in the VM instead of the login tests")
    ap.add_argument("--provision-only", action="store_true")
    ap.add_argument("--power-cut", action="store_true", help="RS-18/T15: power cuts during snapshot writes instead of the reboot/log-out tests")
    ap.add_argument("--panel", action="store_true", help="the panel driven with the VM's keyboard and tablet instead of the reboot/log-out tests")
    ap.add_argument("--hold", type=int, default=0, help="debug: keep the VM up this many seconds after the first login")
    ap.add_argument("--hold-at", default="login", choices=["login", "after-reboot", "end", "fail"])
    a = ap.parse_args()
    global HOLD, HOLD_AT, POWER_CUT, PANEL
    POWER_CUT = a.power_cut
    PANEL = a.panel
    HOLD = min(a.hold, 2400)
    HOLD_AT = a.hold_at
    if a.soak_minutes:
        return soak(a.soak_minutes)
    if not a.session or not a.deb:
        ap.error("--session and --deb are required")
    if a.provision_only:
        base_for(a.session)
        return 0
    try:
        (run_panel if PANEL else run)(a.session, a.deb)
    except Exception as ex:  # noqa: BLE001  record, then still write evidence
        import traceback
        traceback.print_exc()
        check("VXX", "run crashed", False, repr(ex))
    ver = RELEASES[SESSION_RELEASE[a.session]] if a.session else "24.04"
    suite = f"vm-ubuntu{ver}-{a.session}" + ("-power-cut" if POWER_CUT else "") + ("-panel" if PANEL else "")
    ev = {"suite": suite, "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"Ubuntu {ver} desktop VM (QEMU/KVM, 4 vCPU, 6 GiB, virtio-vga 1600x1200, software rendering){' with kde-plasma-desktop (Plasma 5.27)' if a.session == 'plasma-x11' else ' with kde-plasma-desktop (Plasma 6)' if a.session == 'plasma-wayland' else ''}, GDM timed login, {a.session} session, Fractile {Path(a.deb).name} installed with apt",
          "results": results, "log": log_lines}
    out = EVID / f"{suite}.json"
    out.write_text(json.dumps(ev, indent=2) + "\n")
    print(f"{sum(r['ok'] for r in results)}/{len(results)} checks passed; evidence: {out}")
    return 0 if results and all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
