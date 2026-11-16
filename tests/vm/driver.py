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
import shutil
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
RELEASES = {"noble": "24.04", "resolute": "26.04", "arch": "Arch Linux"}
SSH_PORT = 2222
QMP = "/tmp/qmp.sock"
GUEST_TOOL = "/src/tests/vm/guest/vmctl.py"
# session -> (GDM session name, logind session type, the shell process)
SESSIONS = {"wayland": ("ubuntu", "wayland", "gnome-shell"), "x11": ("ubuntu-xorg", "x11", "gnome-shell"),
            "plasma-x11": ("plasma", "x11", "plasmashell"), "plasma-wayland": ("plasma", "wayland", "plasmashell"),
            "sway": ("sway", "wayland", "sway"), "hyprland": ("hyprland", "wayland", "Hyprland"),
            "cosmic": ("cosmic", "wayland", "cosmic-comp")}
# The Ubuntu release each session runs on.
SESSION_RELEASE = {"wayland": "noble", "x11": "noble", "plasma-x11": "noble", "plasma-wayland": "resolute", "sway": "resolute", "hyprland": "arch", "cosmic": "arch"}
PLASMA_VERSIONS = "dpkg-query -W -f='${Package} ${Version}; ' plasma-workspace kwin-wayland kwin-x11 2>/dev/null"
APT = ("sudo DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=60")
APPS_PKGS = ("ubuntu-desktop-minimal linux-generic gnome-terminal gnome-text-editor gnome-calculator "
             "gnome-system-monitor gnome-characters gnome-clocks baobab gnome-disk-utility gnome-font-viewer")

# Arch (Hyprland 0.56 or later; Ubuntu 26.04 has 0.53): the GNOME test apps,
# GDM as the display manager (timed login, as on Ubuntu), Hyprland with
# hyprlock, hypridle and waybar (its tray), software rendering through mesa.
ARCH_PKGS = ("hyprland hyprlock hypridle waybar gdm mesa noto-fonts ttf-dejavu xdg-desktop-portal-hyprland "
             "gnome-terminal gnome-text-editor gnome-calculator gnome-system-monitor gnome-characters gnome-clocks "
             "baobab gnome-disk-utility gnome-font-viewer nautilus python python-gobject")
# Arch with COSMIC (cosmic-session pulls in the whole desktop) for the
# degraded-mode check (T17, T18): no window control, said so, nothing faked.
# vulkan-swrast provides the session's vulkan-driver dependency in a VM.
COSMIC_PKGS = ("cosmic-session vulkan-swrast gdm mesa noto-fonts ttf-dejavu gnome-text-editor python python-gobject")
ARCH_IMG = "https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2"

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


def ensure_arch_image() -> Path:
    """Arch's official cloud image (arch-boxes), checked against its SHA256
    file; the cached copy keeps the build date in its name."""
    sums = urllib.request.urlopen(ARCH_IMG + ".SHA256", timeout=60).read().decode().split()
    want = sums[0]
    img = CACHE / f"arch-cloudimg-{want[:12]}.qcow2"
    if img.exists():
        return img
    part = img.with_suffix(".part")
    say(f"downloading {ARCH_IMG}")
    h = hashlib.sha256()
    with urllib.request.urlopen(ARCH_IMG, timeout=120) as r, open(part, "wb") as f:
        while chunk := r.read(1 << 20):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != want:
        part.unlink()
        raise SystemExit(f"checksum mismatch for the Arch cloud image: {h.hexdigest()} != {want}")
    part.rename(img)
    say(f"Arch cloud image verified (sha256 {want[:16]}...)")
    return img


def ssh_key() -> Path:
    k = CACHE / "id_ed25519"
    if not k.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k)], check=True)
    return k


def seed_arch(path: Path, pkgs: str = ARCH_PKGS) -> Path:
    pub = (ssh_key().with_suffix(".pub")).read_text().strip()
    ud = f"""#cloud-config
hostname: fractile-vm
users:
  - name: tester
    gecos: Test User
    groups: [wheel]
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys: ["{pub}"]
ssh_pwauth: false
runcmd:
  - pacman-key --init && pacman-key --populate archlinux
  - for i in 1 2 3; do pacman -Syu --noconfirm --needed {pkgs} > /var/log/fractile-pacman.log 2>&1 && touch /var/lib/fractile-apps-installed && break; sleep 30; done
  - systemctl enable gdm.service
  - systemctl set-default graphical.target
  - test -e /var/lib/fractile-apps-installed && echo FRACTILE-PROVISION-OK > /dev/ttyS0 || (tail -20 /var/log/fractile-pacman.log > /dev/ttyS0; echo FRACTILE-PROVISION-FAILED > /dev/ttyS0)
power_state:
  mode: poweroff
  timeout: 60
"""
    d = Path("/tmp/seed-arch")
    d.mkdir(exist_ok=True)
    (d / "user-data").write_text(ud)
    (d / "meta-data").write_text("instance-id: fractile-vm-1\nlocal-hostname: fractile-vm\n")
    subprocess.run(["cloud-localds", str(path), str(d / "user-data"), str(d / "meta-data")], check=True)
    return path


def ensure_arch_base(name: str = "hyprland", pkgs: str = ARCH_PKGS) -> Path:
    out = CACHE / f"base-arch-{name}.qcow2"
    if out.exists():
        return out
    img = ensure_arch_image()
    tmp = CACHE / f"base-arch-{name}.part.qcow2"
    subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(img), str(tmp)], check=True)
    subprocess.run(["qemu-img", "resize", str(tmp), "24G"], check=True)
    sd = seed_arch(Path("/tmp/seed-arch.img"), pkgs)
    say(f"provisioning the Arch {name} image: {pkgs} (cloud-init)")
    serial = CACHE / f"provision-arch-{name}-serial.log"
    p = subprocess.Popen(qemu_cmd(tmp, sd, serial))
    try:
        p.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        p.kill()
        raise SystemExit(f"provisioning did not finish in 60 min; see {serial}")
    if "FRACTILE-PROVISION-OK" not in serial.read_text(errors="replace"):
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"Arch provisioning failed (no success marker from cloud-init); see {serial}")
    tmp.rename(out)
    say(f"Arch {name} image ready")
    return out


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


def ensure_sway_base(release: str = "resolute") -> Path:
    """The desktop base plus Ubuntu's sway package (with swaybar, swayidle
    and swaylock), GDM kept as the display manager and offering the "Sway"
    session. The one thing added to Ubuntu's default Sway configuration is
    the lock setup from swayidle(1): the logind Lock and Unlock signals run
    swaylock and end it, so `loginctl lock-session` locks the screen as on
    the other desktops."""
    sfx = "" if release == "noble" else f"-{release}"
    out = CACHE / f"base-sway{sfx}.qcow2"
    if out.exists():
        return out
    base = ensure_base(release)
    say("provisioning the Sway image: sway, swayidle, swaylock on the desktop base")
    vm = VM(base, "sway-provision")
    lock = "exec swayidle -w lock 'swaylock -f -c 1d2330' unlock 'pkill -USR1 -x swaylock'"
    try:
        if not vm.wait_ssh(600):
            raise SystemExit("the Sway provisioning VM did not come up")
        steps = [
            f"for i in 1 2 3; do {APT} update -q && {APT} install -y sway swayidle swaylock > /tmp/sway.log 2>&1 && break; sleep 30; done; tail -3 /tmp/sway.log",
            "test -e /usr/share/wayland-sessions/sway.desktop && grep -q 'config.d' /etc/sway/config",
            f"sudo tee /etc/sway/config.d/60-fractile-test-lock.conf >/dev/null <<'EOF'\n{lock}\nEOF\ncat /etc/sway/config.d/60-fractile-test-lock.conf",
        ]
        for c in steps:
            p = vm.ssh(c, timeout=3600)
            if p.returncode != 0:
                raise SystemExit(f"Sway provisioning step failed: {c} -> {p.returncode}: {(p.stdout + p.stderr).strip()[-600:]}")
        vm.stop()
    finally:
        if vm.p.poll() is None:
            vm.p.kill()
    subprocess.run(["mv", str(vm.disk), str(out)], check=True)
    say("Sway image ready")
    return out


# ---------------------------------------------------------------- VM control

class VM:
    def __init__(self, base: Path, name: str):
        self.disk = Path(f"/tmp/{name}.qcow2")
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(self.disk)], check=True)
        self.serial = Path(f"/tmp/{name}-serial.log")
        self.seed = (seed_arch if "arch" in base.name else seed)(Path("/tmp/run-seed.img"))
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


def compare(before: list[dict], after: list[dict], tol=4, position_only=()) -> tuple[bool, list[str]]:
    """Per app: same number of windows, and each recorded frame matched by a
    distinct window within `tol` px. For apps in `position_only` only the
    position is compared (the app sets its own size later, see
    SELF_SIZING)."""
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
            keys = ("x", "y") if app.removesuffix(".desktop") in position_only else ("x", "y", "w", "h")
            hit = next((h for h in free if h and fr and all(abs(h[k] - fr[k]) <= tol for k in keys)), None)
            if hit is None:
                ok = False
                notes.append(f"{app}: no window at {fr}; have {free}")
            else:
                free.remove(hit)
    extra = [a for a in after if a["app"] not in apps and a["app"]]
    return ok, notes + ([f"other windows: {[a['app'] for a in extra]}"] if extra else [])


# Apps that change their own window size after Fractile placed it, per
# session. gnome-terminal on Sway goes back to its default 80x24 whenever its
# own focus changes (in or out), long after the placement (reproduced by
# hand: place a terminal, focus another window, it is 573x260 again). For
# these the layout check compares the position, and the restore report must
# show the window placed at the recorded frame.
SELF_SIZING = {"sway": ("org.gnome.Terminal",)}


def placed_as_reported(vm: VM, apps: tuple, tol=4) -> tuple[bool, list[str]]:
    """The last restore report: each item of these apps placed at its recorded
    frame (observed vs requested)."""
    if not apps:
        return True, []
    rep = json.loads(vm.ssh("XDG_RUNTIME_DIR=/run/user/$(id -u) fractile --json restore-status").stdout or "{}")
    items = [i for i in (((rep.get("last_report") or {}).get("report") or {}).get("items") or [])
             if any(i.get("label", "").split(" #")[0] == a for a in apps)]
    bad = [f"{i['label']} placed {i.get('observed')} for {i.get('requested')}" for i in items
           if not (i.get("observed") and i.get("requested") and all(abs(i["observed"][k] - i["requested"][k]) <= tol for k in ("x", "y", "w", "h")))]
    return bool(items) and not bad, ([f"{', '.join(apps)}: {len(items)} placed at the recorded frame by the restore, then resized by the app itself"] if items and not bad else bad or [f"no restore report items for {apps}"])


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
    gdm_conf = "/etc/gdm3/custom.conf"
    if SESSION_RELEASE.get(session) == "arch":
        # GDM 51 (Arch) refuses a timed login unless automatic login is on
        # for the same user ("refusing auto login for user 'tester'
        # (enabled=0, allowed=)"); with both, it logs in at boot and again
        # after each log-out (checked by hand in the VM).
        gdm_conf = "/etc/gdm/custom.conf"
        conf = conf.replace("AutomaticLoginEnable=False", "AutomaticLoginEnable=True\\nAutomaticLogin=tester")
    vm.ssh(f"sudo mkdir -p /var/lib/AccountsService/users && printf '{conf}' | sudo tee {gdm_conf} >/dev/null && printf '{acct}' | sudo tee /var/lib/AccountsService/users/tester >/dev/null", check_rc=True)


HYPR_CONFIG = """-- What a Hyprland user without uwsm sets up (docs/compatibility/hyprland.md):
-- the start line for Fractile, a bar with a tray, and the lock daemon.
hl.monitor({ output = "", mode = "1600x1200@60", position = "0x0", scale = 1 })
hl.config({ misc = { disable_hyprland_logo = true, disable_splash_rendering = true } })
hl.on("hyprland.start", function()
  hl.exec_cmd("waybar")
  hl.exec_cmd("hypridle")
  hl.exec_cmd("fractiled --autostart --compositor-start")
end)
"""
# hypridle(1): the logind Lock and Unlock signals start hyprlock and end it,
# so `loginctl lock-session` locks the screen as on the other desktops.
HYPRIDLE_CONFIG = """general {
    lock_cmd = pidof hyprlock || hyprlock
    unlock_cmd = pkill -USR1 -x hyprlock
}
"""


def hyprland_user_config(vm: VM) -> None:
    vm.ssh("mkdir -p ~/.config/hypr", check_rc=True)
    for name, text in (("hyprland.lua", HYPR_CONFIG), ("hypridle.conf", HYPRIDLE_CONFIG)):
        p = subprocess.run(["ssh", "-q", "-i", str(vm.ssh_key), "-p", str(SSH_PORT), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                            "tester@127.0.0.1", f"cat > ~/.config/hypr/{name}"], input=text, text=True, capture_output=True, timeout=60)
        if p.returncode != 0:
            raise RuntimeError(f"writing ~/.config/hypr/{name}: {p.stderr.strip()}")
    # hyprlock's own example configuration, if the package ships one
    vm.ssh("test -e ~/.config/hypr/hyprlock.conf || cp /usr/share/hypr/hyprlock.conf ~/.config/hypr/ 2>/dev/null; true")


def base_for(session: str) -> Path:
    rel = SESSION_RELEASE[session]
    if session == "sway":
        return ensure_sway_base(rel)
    if session == "hyprland":
        return ensure_arch_base()
    if session == "cosmic":
        return ensure_arch_base("cosmic", COSMIC_PKGS)
    return ensure_plasma_base(rel) if session.startswith("plasma") else ensure_base(rel)


def run(session: str, deb: str) -> None:
    base = base_for(session)
    _xs, stype, shell = SESSIONS[session]
    plasma = session.startswith("plasma")
    arch = SESSION_RELEASE[session] == "arch"
    wlr = session in ("sway", "hyprland")  # IPC compositors: no extension, a locker process, a bar's tray
    ver = RELEASES[SESSION_RELEASE[session]]
    vm = VM(base, f"run-{session}")
    try:
        if not check("V00", "the VM boots and answers over ssh", vm.wait_ssh(600)):
            return
        gdm_and_session(vm, session)
        vm.scp(deb, "/tmp/fractile.pkg.tar.zst" if arch else "/tmp/fractile.deb")
        vm.scp(GUEST_TOOL, "/home/tester/vmctl.py")
        ver_cmd = PLASMA_VERSIONS if plasma else "sway --version" if session == "sway" else "Hyprland --version | head -1" if session == "hyprland" else "gnome-shell --version"
        rel = vm.ssh(f". /etc/os-release; echo $PRETTY_NAME; {ver_cmd}; uname -r").stdout.strip().replace("\n", "; ")
        if session == "hyprland":
            hyprland_user_config(vm)
        if arch:
            inst = vm.ssh("for i in 1 2 3; do sudo pacman -U --noconfirm /tmp/fractile.pkg.tar.zst > /tmp/pacman.log 2>&1 && break; "
                          "echo \"attempt $i failed\"; sleep 30; sudo pacman -Sy --noconfirm > /dev/null 2>&1; done; tail -4 /tmp/pacman.log; pacman -Q fractile", timeout=1800)
            check("V01", "pacman installs the Arch package (dependencies from the Arch repositories)",
                  inst.returncode == 0 and "fractile 0.9.0" in inst.stdout, f"{rel} :: {inst.stdout.strip()[-300:]}")
        else:
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
              bool(up) and setup.get("unit_enabled") == "enabled" and setup.get("policy") == "last_clean" and setup.get("extension") == ("n/a" if session in ("plasma-x11", "sway", "hyprland") else 0),
              f"session {s1.get('session_type')}, service socket {bool(up)}; {json.dumps(setup)[:400]}")

        # One real log-out and log-in (loads the extension on Wayland).
        shell_before = vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()
        # One log-out saves one Logout snapshot (the display closing and
        # SIGTERM arrive together on Hyprland and once both saved one).
        count_saved = lambda: int((vm.ssh("grep -a -c -E 'Logout snapshot [0-9a-f-]+ saved|as Logout snapshot' ~/.local/state/fractile/fractile.log").stdout.strip() or "0").splitlines()[-1])
        saved0 = count_saved()
        lo = vm.vmctl("logout")
        relog = wait_for(lambda: (lambda pid: pid and pid != shell_before)(vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()), 180, 3)
        s2 = wait_session(vm, stype)
        st2 = s2.get("status") or {}
        # How the service was started at this login: the systemd unit, or (in a
        # session that never reaches graphical-session.target, such as
        # Ubuntu's plain Sway session) the packaged compositor configuration.
        gst = vm.ssh("systemctl --user is-active graphical-session.target").stdout.strip()
        how = vm.ssh("grep -a 'compositor start' ~/.local/state/fractile/fractile.log | tail -1").stdout.strip()
        by_compositor = "starting the service from the compositor configuration" in how
        started = s2.get("unit_active") == "active" or (wlr and by_compositor)
        check("V03", "after a real log-out and the next login, the service started by itself (the systemd user unit, or on Sway/Hyprland without "
                     "graphical-session.target the compositor configuration: Sway's packaged snippet, Hyprland's documented start line) and it controls windows",
              bool(relog) and started and bool(st2.get("window_control")),
              f"logout {lo}; new shell {bool(relog)}; unit {s2.get('unit_active')}/{s2.get('unit_enabled')}; graphical-session.target {gst}; "
              f"{how[-160:]}; adapter {st2.get('adapter')} window_control {st2.get('window_control')} session {s2.get('session_type')}")
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

        # Real reboot. "Where they were" is the layout at the reboot: an app
        # may change its own window after it was placed (see SELF_SIZING).
        at_reboot = [w for w in (vm.vmctl("state").get("windows") or []) if w.get("app") in apps] or before
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
        selfsz = SELF_SIZING.get(session, ())
        ok, notes = compare(at_reboot, after.get("windows") or [], position_only=selfsz)
        rep_ok, rep_notes = placed_as_reported(vm, selfsz)
        ok &= rep_ok
        notes += rep_notes
        if len(at_reboot) != 15:
            ok = False
            notes.append(f"{len(at_reboot)} windows open at the reboot, expected 15")
        moved = [f"{b['app']} {b['frame']} at the reboot vs {a.get('frame')} when placed" for b in at_reboot for a in before
                 if a["id"] == b["id"] and a.get("frame") != b.get("frame")] if at_reboot is not before else []
        if moved:
            notes.append("changed by the app after placement: " + "; ".join(moved))
        runs = (after.get("restore") or {}).get("recent_runs") or [{}]
        check("V05", "after a real reboot and the next login, Fractile asks first (the default); Enter on the prompt restores all 15 windows of the 10 apps where they were, with no duplicates",
              ok and s3.get("boot_id") != boot0 and runs[0].get("state") == "completed" and pressed,
              f"boot id changed {s3.get('boot_id') != boot0}; asked {bool(asked)}, Enter pressed {pressed} ({ask_shot}); last restore {runs[0].get('state')} {runs[0].get('summary')}; {'; '.join(notes)[:600]}; {shot}")

        # The user turns off "Show what will be restored and ask first".
        vm.ssh("XDG_RUNTIME_DIR=/run/user/$(id -u) fractile config set session.confirm_before_restore false", check_rc=True)

        # Real log-out and timed re-login.
        at_logout = [w for w in (vm.vmctl("state").get("windows") or []) if w.get("app") in apps] or before
        shell_before = vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()
        lo = vm.vmctl("logout")
        relog = wait_for(lambda: (lambda pid: pid and pid != shell_before)(vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()), 180, 3)
        wait_session(vm, stype)
        after2 = wait_restore_done(vm, not_run=runs[0].get("run_id"))
        shot = vm.screenshot(f"{session}-03-after-logout")
        decision = vm.ssh("grep -a -E 'starting pid|login session|login restore|snapshot|capture|previous session|stopping|SIGTERM|signal' ~/.local/state/fractile/fractile.log | tail -14").stdout.strip()
        ok2, notes2 = compare(at_logout, after2.get("windows") or [], position_only=selfsz)
        rep_ok2, rep_notes2 = placed_as_reported(vm, selfsz)
        ok2 &= rep_ok2
        notes2 += rep_notes2
        if len(at_logout) != 15:
            ok2 = False
            notes2.append(f"{len(at_logout)} windows open at the log-out, expected 15")
        saved = count_saved() - saved0
        if saved != 1:
            ok2 = False
            notes2.append(f"{saved} Logout snapshots saved at the log-out, expected 1")
        runs2 = (after2.get("restore") or {}).get("recent_runs") or [{}]
        check("V06", "with asking turned off, after a real log-out and the next login the same 15 windows are restored automatically, no duplicates; the log-out saved one Logout snapshot",
              ok2 and bool(relog) and runs2[0].get("state") == "completed" and runs2[0].get("source") == "login",
              f"logout {lo.get('rc')}; new login {bool(relog)}; last restore {runs2[0].get('state')} source {runs2[0].get('source')} {runs2[0].get('summary')}; {'; '.join(notes2)[:600]}; {shot}; log: {decision[-2500:]}")
        # The apps Fractile reopened must not inherit anything from the
        # service: no-new-privileges stops sudo in a restored terminal, and a
        # seccomp filter made GNOME Disks abort on Arch. Fractile's own
        # umask is 077; the apps get the session's. Every running test-app
        # process is checked wherever it runs (Fractile's scopes, or the bus
        # and the desktop's own session restore for D-Bus-activated apps),
        # and so is the first process still in each of Fractile's scopes.
        probe = vm.ssh(r"""for c in /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/app.slice/app-fractile-*.scope; do
          p=$(head -1 $c/cgroup.procs 2>/dev/null); [ -n "$p" ] && echo "scope $(cat /proc/$p/comm) $(grep -E '^(NoNewPrivs|Seccomp|Umask):' /proc/$p/status | tr -s '\t\n' '  ')"; done
        for n in gnome-terminal- gnome-text-edit gnome-calculato gnome-system-mo gnome-character gnome-clocks baobab gnome-disks gnome-font-view nautilus; do
          for p in $(pgrep -u tester -x $n); do echo "app $(cat /proc/$p/comm) $(grep -E '^(NoNewPrivs|Seccomp|Umask):' /proc/$p/status | tr -s '\t\n' '  ')"; done; done
        p=$(pgrep -u tester -x fractiled | head -1); echo "fractiled $(grep -E '^(NoNewPrivs|Seccomp|Umask):' /proc/$p/status | tr -s '\t\n' '  ')"
        echo "session $(grep -E '^Umask:' /proc/$(pgrep -u tester -x systemd | head -1)/status | cut -f2)" """).stdout.strip().splitlines()
        # How the last login restore started its apps: an app started in a
        # scope is Fractile's child; one activated over D-Bus (Plasma, GNOME)
        # is started by the bus and inherits nothing from the service.
        launch_log = vm.ssh("awk '/login restore decision/{n=NR} {l[NR]=$0} END{for(i=n;i<=NR;i++) print l[i]}' ~/.local/state/fractile/fractile.log | grep -a -o ' launch [^ ]* via [a-z+-]*'").stdout.split("\n")
        scoped = sorted({l.split()[1] for l in launch_log if "via systemd-scope" in l})
        bus = sorted({l.split()[1] for l in launch_log if l.strip() and "via systemd-scope" not in l})
        scopes_p = [l for l in probe if l.startswith("scope ")]
        apps_p = [l for l in probe if l.startswith("app ")]
        sess_umask = next((l.split()[1] for l in probe if l.startswith("session ")), "?")
        bad = [l for l in apps_p + scopes_p if "NoNewPrivs: 1" in l or "Seccomp: 0" not in l or f"Umask: {sess_umask}" not in l]
        own = next((l for l in probe if l.startswith("fractiled ")), "")
        check("V09", "the apps Fractile reopened run without restrictions from the service (no-new-privileges 0, no seccomp filter, the umask of the user's systemd manager); fractiled's own umask is 0077",
              len(apps_p) >= 10 and not bad and "Umask: 0077" in own,
              f"{len(apps_p)} test-app processes running (wherever they were started), {len(scopes_p)} processes still leading Fractile's scopes, session umask {sess_umask}; not clean: {bad[:4]}; {own}; "
              f"the login restore started {len(scoped)} apps in scopes and {len(bus)} another way ({', '.join(bus)[:200]})")
        # real lock screen: logind asks the session's own locker (GNOME Shell's
        # shield, KDE's kscreenlocker) to lock; the locker sets LockedHint
        # the user's graphical session (older sessions from earlier log-outs can linger as "closing")
        sid = vm.ssh("loginctl show-user tester -p Display --value").stdout.strip()
        vm.ssh(f"sudo loginctl lock-session {sid}")
        locked = wait_for(lambda: (vm.call("status").get("result") or {}).get("locked") or None, 30)
        hint = vm.ssh(f"loginctl show-session {sid} -p LockedHint --value").stdout.strip()
        # Sway's locker (swaylock through swayidle) does not set LockedHint:
        # there the screen is locked while swaylock runs.
        locker_name = {"sway": "swaylock", "hyprland": "hyprlock"}.get(session, "")
        locker = vm.ssh(f"pgrep -u tester -x {locker_name}").stdout.strip() if wlr else ""
        really_locked = bool(locker) if wlr else hint == "yes"
        shot_l = vm.screenshot(f"{session}-04-locked")
        snaps = vm.ssh("XDG_RUNTIME_DIR=/run/user/$(id -u) fractile --json list-snapshots").stdout
        snap_id = (json.loads(snaps or "[]") or [{}])[0].get("id", "")
        rr = vm.ssh(f"XDG_RUNTIME_DIR=/run/user/$(id -u) fractile restore --snapshot {snap_id} 2>&1")
        vm.ssh(f"sudo loginctl unlock-session {sid}")
        unlocked = wait_for(lambda: (not (vm.call("status").get("result") or {}).get("locked")) or None, 30)
        check("V07", "the desktop's real lock screen (loginctl lock-session): Fractile reports the session locked and refuses a restore; after unlocking it works again",
              bool(locked) and really_locked and rr.returncode != 0 and "locked" in (rr.stdout + rr.stderr).lower() and bool(unlocked),
              f"session {sid}; LockedHint {hint}{f'; {locker_name} pid ' + locker if wlr else ''}; status locked {bool(locked)}; restore exit {rr.returncode}: {(rr.stdout + rr.stderr).strip()[:160]}; unlocked {bool(unlocked)}; {shot_l}")
        if wlr:
            # swaybar's tray (waybar's on Hyprland) (its own StatusNotifierWatcher): the icon, and a
            # click on it opens the panel for the focused window. Qt reports
            # NeedsAttention for the timeout of a message it shows (the
            # restore notification, 6 s), then Active again.
            seen = []

            def tray_active():
                t = vm.vmctl("tray")
                seen.append((t.get("status") or "?").strip("(<'>,)"))
                return t if t.get("registered") and "Active" in (t.get("status") or "") else None
            tr = wait_for(tray_active, 20, 1) or vm.vmctl("tray")
            shot_t = vm.screenshot(f"{session}-05-tray")
            wins = vm.call("list_windows").get("result") or []
            if wins:
                vm.call("activate_window", {"window": wins[0]["id"]})
                time.sleep(0.8)
            act = vm.vmctl("tray-activate")
            panel = wait_for(lambda: next((w for w in (vm.call("list_windows", {"all": True}).get("result") or [])
                                           if (w.get("title") or "").startswith("Fractile panel")), None), 15, 1)
            shot_p = vm.screenshot(f"{session}-06-tray-panel")
            bar = "swaybar" if session == "sway" else "waybar"
            check("V08", f"{session}: Fractile's icon is in {bar}'s tray (StatusNotifierWatcher), active; a click on it (StatusNotifierItem.Activate) opens the panel",
                  bool(tr.get("registered")) and "Active" in (tr.get("status") or "") and bool(act.get("activated")) and bool(panel),
                  f"{json.dumps(tr)[:240]}; status seen {' > '.join(dict.fromkeys(seen))}; {act}; panel {panel and panel.get('frame')}; {shot_t}; {shot_p}")
            vm.key("esc")
        hold("end")
        logs = vm.ssh("XDG_RUNTIME_DIR=/run/user/$(id -u) fractile logs --redacted --lines 2000 2>&1 | grep -iE 'warn|error|restore| place | launch ' | tail -200").stdout
        (EVID / "vm").mkdir(parents=True, exist_ok=True)
        (EVID / "vm" / f"{session}-fractile-log.txt").write_text(logs)
    finally:
        vm.stop()


def screen_changed(a: str, b: str, top: int = 60) -> float:
    """Fraction of pixels that differ between two screenshots, below the
    top panel (its clock changes on its own)."""
    from PIL import Image, ImageChops
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    w, h = ia.size
    box = (0, top, w, h)
    diff = ImageChops.difference(ia.crop(box), ib.crop(box)).convert("L").point(lambda v: 255 if v > 24 else 0)
    return sum(1 for v in diff.getdata() if v) / ((h - top) * w)


def settled_shot(vm: VM, name: str, timeout: float = 60) -> str:
    """A screenshot once the screen has stopped changing (a window that is
    still opening, or being tiled, would otherwise count as a change)."""
    prev, end = None, time.time() + timeout
    while True:
        cur = vm.screenshot(name)
        if cur.startswith("(") or time.time() > end:
            return cur
        local = cur.replace("tests/evidence/", "/evidence/", 1)
        if prev and screen_changed(prev, local) < 0.0005:
            return cur
        prev = f"/tmp/settle-{name}.png"
        shutil.copy(local, prev)
        time.sleep(2)


def run_cosmic(deb: str) -> None:
    """T17/T18 and SRS Phase 5 in a real COSMIC login. COSMIC has no client
    API to move or resize other apps' windows: Fractile must say so (status,
    diagnose, the tray click), refuse placing and snapping with the reason,
    change nothing on screen and claim no placement. What COSMIC does offer
    (the window list, workspace moves, maximize) is used: a session restore
    after a real reboot reopens the approved app once and puts it back on its
    workspace, maximized, reported as RESTORED_WITHOUT_GEOMETRY. COSMIC's
    settings are never touched (auto-tiling is on here, as a user who tiles
    would have it)."""
    session = "cosmic"
    vm = VM(base_for(session), "run-cosmic")
    try:
        if not check("V00", "the VM boots and answers over ssh", vm.wait_ssh(600)):
            return
        gdm_and_session(vm, session)
        vm.scp(deb, "/tmp/fractile.pkg.tar.zst")
        vm.scp(GUEST_TOOL, "/home/tester/vmctl.py")
        rel = vm.ssh(". /etc/os-release; echo $PRETTY_NAME; pacman -Q cosmic-comp cosmic-session; uname -r").stdout.strip().replace("\n", "; ")
        # COSMIC's own setting (Settings > Desktop > Workspaces > auto-tile),
        # as cosmic-config stores it.
        vm.ssh("mkdir -p ~/.config/cosmic/com.system76.CosmicComp/v1 && printf true > ~/.config/cosmic/com.system76.CosmicComp/v1/autotile", check_rc=True)
        inst = vm.ssh("for i in 1 2 3; do sudo pacman -U --noconfirm /tmp/fractile.pkg.tar.zst > /tmp/pacman.log 2>&1 && break; "
                      "echo \"attempt $i failed\"; sleep 30; sudo pacman -Sy --noconfirm > /dev/null 2>&1; done; tail -4 /tmp/pacman.log; pacman -Q fractile", timeout=1800)
        check("V01", "pacman installs the Arch package (dependencies from the Arch repositories)",
              inst.returncode == 0 and "fractile 0.9.0" in inst.stdout, f"{rel} :: {inst.stdout.strip()[-300:]}")

        vm.ssh("sudo systemctl reboot", timeout=20)
        vm.wait_down()
        vm.wait_ssh(600)
        s1 = wait_session_shell(vm, "wayland")
        hold("login")
        if not check("C01", "a real COSMIC login: GDM logs in to the COSMIC session and cosmic-comp runs",
                     bool(s1.get("shell_pid")) and "COSMIC" in (s1.get("desktop") or ""),
                     f"{rel}; session {s1.get('graphical_session')} desktop {s1.get('desktop')} display {s1.get('display')} cosmic-comp pid {s1.get('shell_pid')}"):
            vm.screenshot("cosmic-00-no-login")
            return
        vm.vmctl("launch io.fractile.Fractile.desktop")
        up = wait_for(lambda: vm.ssh("test -S /run/user/$(id -u)/fractile/fractile.sock").returncode == 0, 60)
        setup = vm.vmctl("setup")
        b = vm.vmctl("cosmic-before", timeout=300)
        st = b.get("status") or {}
        reason = "no client API to move or resize"
        check("C02", "started from the app menu, Fractile runs with the COSMIC adapter and says what it can do: window placement not available "
                     "(the reason in diagnose), session restore without positions or sizes, COSMIC auto-tiling detected",
              bool(up) and st.get("adapter") == "cosmic" and st.get("window_control") is False and st.get("restore_without_geometry") is True
              and st.get("tiling_manager") == "cosmic-autotile"
              and "NOT AVAILABLE" in (b.get("status_text") or "") and reason in (b.get("diagnose") or ""),
              f"socket {bool(up)}; setup {json.dumps(setup)[:200]}; adapter {st.get('adapter')} window_control {st.get('window_control')} "
              f"restore_without_geometry {st.get('restore_without_geometry')} tiling {st.get('tiling_manager')}; "
              f"status: {(b.get('status_text') or '')[:400]!r}; diagnose rc {b.get('diagnose_rc')}: {(b.get('diagnose') or '')[:600]!r}")
        shot_a = settled_shot(vm, "cosmic-01-before")
        t = vm.vmctl("cosmic-try", timeout=300)
        time.sleep(2)
        shot_b = settled_shot(vm, "cosmic-02-after")
        outs = (t.get("list-outputs") or {}).get("json") or []
        eds = t.get("editors") or []
        wss = (t.get("workspaces") or {}).get("result") or []

        def inside(fr, o):
            g = o.get("rect") or {}
            return bool(fr and g) and g["x"] <= fr["x"] and g["y"] <= fr["y"] and fr["x"] + fr["w"] <= g["x"] + g["w"] + 1 and fr["y"] + fr["h"] <= g["y"] + g["h"] + 1
        ed0 = eds[0] if eds else {}
        check("C03", "the COSMIC adapter lists the monitor, the workspaces and the text editor window (its app, workspace and a frame on the monitor)",
              (t.get("list-windows") or {}).get("rc") == 0 and len(outs) >= 1 and len(eds) == 1 and len(wss) >= 2
              and ed0.get("workspace") == 0 and any(inside(ed0.get("frame"), o) for o in outs),
              f"outputs {[(o.get('stable_id'), o.get('rect')) for o in outs]}; workspaces {wss}; "
              f"editor {[(e.get('id'), e.get('desktop_file_id'), e.get('workspace'), e.get('frame'), e.get('state')) for e in eds]}; "
              f"list-windows rc {(t.get('list-windows') or {}).get('rc')} {(t.get('list-windows') or {}).get('stderr', '')[:200]!r}")
        refused = {k: (t.get(k) or {}).get("rc") != 0 and reason in ((t.get(k) or {}).get("stderr", "") + (t.get(k) or {}).get("stdout", ""))
                   for k in ("place", "snap")}
        ok_shots = not (shot_a.startswith("(") or shot_b.startswith("("))
        frac = screen_changed(*(x.replace("tests/evidence/", "/evidence/", 1) for x in (shot_a, shot_b))) if ok_shots else 1.0
        check("C04", "no false success (T17): placing and snapping fail with the reason, and nothing moved (the screen below the panel clock is the same)",
              all(refused.values()) and b.get("editor") and frac < 0.002,
              f"refused with the reason {refused}; text editor open {b.get('editor')}; {frac:.5f} of pixels changed; {shot_a}, {shot_b}; "
              + "; ".join(f"{k}: {((t.get(k) or {}).get('stderr') or (t.get(k) or {}).get('stdout') or '')[:200]!r}" for k in ("place", "snap")))

        tray = vm.vmctl("tray")
        check("C06", "the tray icon is registered with COSMIC's panel (StatusNotifierItem)", tray.get("registered") and "Active" in (tray.get("status") or ""),
              json.dumps(tray)[:300])
        # A click on the tray icon (what the panel's status area sends) must
        # show something: here, that windows cannot be placed and why.
        before_click = settled_shot(vm, "cosmic-03-before-tray-click")
        act = vm.vmctl("tray-activate")
        time.sleep(3)
        after_click = settled_shot(vm, "cosmic-04-tray-click")
        ok_shots = not (before_click.startswith("(") or after_click.startswith("("))
        shown = screen_changed(*(x.replace("tests/evidence/", "/evidence/", 1) for x in (before_click, after_click))) if ok_shots else 0.0
        check("C08", "a click on the tray icon shows a window that says windows cannot be placed on this desktop (no silent nothing, no grid that cannot work)",
              act.get("activated") and shown > 0.01, f"activated {act}; {shown:.4f} of the screen changed; {after_click}")

        # What COSMIC does allow: the editor to the second workspace and
        # maximized (Fractile's requests, carried out by cosmic-comp), saved.
        ar = vm.vmctl("cosmic-arrange", timeout=300)
        aft = (ar.get("after") or [{}])[0] if ar.get("after") else {}
        recs = [r for r in (ar.get("records") or []) if r.get("app") == "org.gnome.TextEditor.desktop"]
        check("C09", "the editor is moved to the second workspace and maximized on request (COSMIC may confirm a window on a workspace not in view only "
                     "later, and the request says so), and a saved snapshot records that workspace and state",
              "error" not in (ar.get("move") or {"error": 1})
              and ("error" not in (ar.get("maximize") or {"error": 1}) or "not confirmed it yet" in str((ar.get("maximize") or {}).get("error")))
              and aft.get("workspace") == 1 and (aft.get("state") or {}).get("maximized") and bool(ar.get("snapshot_id"))
              and len(recs) == 1 and recs[0].get("workspace_id") == "ws-1" and (recs[0].get("state") or {}).get("maximized"),
              f"move {ar.get('move')}; maximize {ar.get('maximize')} (state seen after {ar.get('confirmed_after_s')} s); after {[(e.get('workspace'), e.get('state'), e.get('frame')) for e in (ar.get('after') or [])]}; "
              f"snapshot {ar.get('snapshot_id')} records {recs}; workspaces {ar.get('workspaces')}")
        shot_ar = settled_shot(vm, "cosmic-05-arranged")
        cfg_a, cfg_b = b.get("config") or {}, ar.get("config") or {}
        changed = sorted(k for k in set(cfg_a) | set(cfg_b) if cfg_a.get(k) != cfg_b.get(k))
        auto = vm.ssh("cat ~/.config/cosmic/com.system76.CosmicComp/v1/autotile").stdout.strip()
        check("C05", "COSMIC's settings are untouched (T18): auto-tiling still on, no file under ~/.config/cosmic written while Fractile listed, "
                     "refused, moved to a workspace, maximized and saved",
              auto == "true" and not changed, f"autotile {auto!r}; {len(cfg_b)} files; changed {changed[:10]}; {shot_ar}")

        # Restore without asking first, then a real reboot.
        vm.ssh("XDG_RUNTIME_DIR=/run/user/$(id -u) fractile config set session.confirm_before_restore false", check_rc=True)
        boot0 = vm.vmctl("session").get("boot_id")
        vm.ssh("sudo systemctl reboot", timeout=20)
        vm.wait_down()
        vm.wait_ssh(600)
        wait_session_shell(vm, "wayland")
        up2 = wait_for(lambda: (vm.vmctl("session").get("status") or {}).get("adapter") == "cosmic", 120, 3)
        s2 = vm.vmctl("session")
        gst = vm.ssh("systemctl --user is-active graphical-session.target").stdout.strip()
        st2 = s2.get("status") or {}
        check("C07", "after a real reboot the service starts at login by itself (the systemd unit) with the COSMIC adapter",
              bool(up2) and s2.get("unit_active") == "active" and st2.get("adapter") == "cosmic" and s2.get("boot_id") != boot0,
              f"boot id changed {s2.get('boot_id') != boot0}; unit {s2.get('unit_active')}/{s2.get('unit_enabled')}; graphical-session.target {gst}; "
              f"adapter {st2.get('adapter')} window_control {st2.get('window_control')}")
        wait_restore_done(vm)
        time.sleep(5)
        af = vm.vmctl("cosmic-after", timeout=300)
        shot_r = settled_shot(vm, "cosmic-06-after-reboot")
        run_ = af.get("run") or {}
        items = [i for i in (run_.get("items") or []) if "TextEditor" in (i.get("record_id", "") + (i.get("label") or "") + (i.get("window_id") or ""))
                 or len(run_.get("items") or []) == 1]
        eds2 = af.get("editors") or []
        e2 = eds2[0] if eds2 else {}
        srcs = af.get("source_records") or []
        src = srcs[0] if len(srcs) == 1 else {}
        check("C10", "after the reboot Fractile reopens the approved editor once and puts it back on the second workspace, maximized as the snapshot it "
                     "restored from records; the item says RESTORED_WITHOUT_GEOMETRY (position and size not restored), never placed",
              len(eds2) == 1 and af.get("procs") == 1 and e2.get("workspace") == 1 and src.get("workspace_id") == "ws-1"
              and (src.get("state") or {}).get("maximized") is True and (e2.get("state") or {}).get("maximized") is True
              and run_.get("state") in ("completed", "partial") and items
              and all(i.get("status") == "RESTORED_WITHOUT_GEOMETRY" and "not restored" in (i.get("message") or "") for i in items),
              f"editors {[(e.get('id'), e.get('workspace'), e.get('state')) for e in eds2]}; processes {af.get('procs')}; source records {srcs}; "
              f"run {run_.get('state')} {run_.get('summary')} from {run_.get('source')}; items {[(i.get('status'), (i.get('message') or '')[:160], i.get('launched_at')) for i in (run_.get('items') or [])]}; {shot_r}")
        again = vm.vmctl(f"cosmic-restore-again {shlex.quote(ar.get('snapshot_id') or 'latest')}", timeout=300)
        rr = ((again.get("restore") or {}).get("result") or {})
        rep_items = rr.get("items") or ((rr.get("report") or {}).get("items")) or []
        check("C11", "restoring the same snapshot again opens nothing new (no duplicates) and claims no placement",
              len(again.get("editors") or []) == 1 and again.get("procs") == 1
              and not any(i.get("status") in ("LAUNCHED_AND_PLACED", "PLACED_ONLY") or i.get("launched") for i in rep_items),
              f"editors {len(again.get('editors') or [])}; processes {again.get('procs')}; "
              f"items {[(i.get('status'), i.get('launched')) for i in rep_items]}; {json.dumps(again.get('restore'))[:300]}")
        log = vm.ssh("grep -a -E 'warn|error|cosmic|unsupported|restore' ~/.local/state/fractile/fractile.log | tail -80").stdout
        log_lines.extend(log.splitlines()[-80:])
        hold("end")
    finally:
        vm.p.kill()


def run_upgrade(session: str, old_debs: list[str], deb: str) -> None:
    """PK-06 / SRS 9: upgrade a real install in a real login, keeping settings
    and snapshots. old_debs are earlier builds (oldest first); the first is
    installed and used, each next one is installed over it, then `deb`.

    An upgrade from a build without the service's self-restart (before
    ef1d4e3) needs the service restarted; the package says so, and the run
    does what a user does: log out and in. From a build with it, the service
    restarts onto the new binary in place inside the running session."""
    base = base_for(session)
    _xs, stype, shell = SESSIONS[session]
    plasma = session.startswith("plasma")
    vm = VM(base, f"upgrade-{session}")

    def ver_of(path):
        return subprocess.run(["dpkg-deb", "-f", path, "Version"], capture_output=True, text=True).stdout.strip()

    def status():
        return vm.call("status").get("result") or {}

    def cfg():
        return (vm.call("get_config").get("result") or {}).get("config") or {}

    def relogin():
        before = vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()
        vm.vmctl("logout")
        wait_for(lambda: (lambda pid: pid and pid != before)(vm.ssh(f"pgrep -u tester -x {shell}").stdout.strip()), 180, 3)
        return wait_session(vm, stype)

    def install(path, tag):
        vm.scp(path, f"/tmp/{tag}.deb")
        return vm.ssh(f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y /tmp/{tag}.deb > /tmp/apt-{tag}.log 2>&1; echo rc=$?; "
                      f"grep -i 'fractile:' /tmp/apt-{tag}.log | head -3; dpkg-query -W fractile", timeout=900)

    def frame(wid):
        return next((x["frame"] for x in (vm.call("list_windows", {"all": True}).get("result") or []) if x["id"] == wid), None)

    try:
        if not check("U00", "the VM boots and answers over ssh", vm.wait_ssh(600)):
            return
        gdm_and_session(vm, session)
        vm.scp(GUEST_TOOL, "/home/tester/vmctl.py")
        first = old_debs[0]
        v0 = ver_of(first)
        vm.scp(first, "/tmp/old0.deb")
        inst = vm.ssh("for i in 1 2 3; do sudo DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 "
                      "-o Acquire::http::Timeout=60 install -y /tmp/old0.deb > /tmp/apt.log 2>&1 && break; sleep 30; "
                      "sudo apt-get -o Acquire::ForceIPv4=true update -q > /dev/null 2>&1; done; dpkg-query -W fractile", timeout=1800)
        if not check("U01", f"the old build installs ({v0})", inst.returncode == 0 and f"\t{v0}" in inst.stdout, inst.stdout.strip()[-200:]):
            return
        vm.ssh("sudo systemctl reboot", timeout=20)
        vm.wait_down()
        vm.wait_ssh(600)
        wait_session_shell(vm, stype)
        vm.vmctl("launch io.fractile.Fractile.desktop")
        wait_for(lambda: vm.ssh("test -S /run/user/$(id -u)/fractile/fractile.sock").returncode == 0, 60)
        setup = vm.vmctl("setup")
        relogin()
        st = status()
        if not check("U02", f"the old build runs in a real {session} login and controls windows",
                     st.get("version") == v0.split("-")[0] and (vm.vmctl("session").get("status") or {}).get("window_control"),
                     f"version {st.get('version')}; setup {json.dumps(setup)[:200]}"):
            return

        # The user's settings, a shortcut of their own and a named snapshot.
        c = cfg()
        c["appearance"]["default_grid"] = {"cols": 8, "rows": 5}
        c["general"]["panel_shortcut"] = "Ctrl+Alt+P"
        c["shortcuts"].append({"id": "u-top-left", "name": "Upgrade test top left", "scope": "global", "accelerator": "Ctrl+Alt+Shift+F7",
                               "cycle_monitors": False, "action": {"type": "place", "grid": {"cols": 2, "rows": 2},
                                                                    "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}})
        vm.call("set_config", {"config": c})
        vm.vmctl("launch org.gnome.TextEditor.desktop")
        ed = wait_for(lambda: next((w for w in (vm.call("list_windows").get("result") or []) if w.get("desktop_file_id") == "org.gnome.TextEditor.desktop"), None), 60, 2)
        if not check("U03", "settings, a shortcut and a window to snapshot are set up on the old build", bool(ed), f"editor {bool(ed)}"):
            return
        wid = ed["id"]
        vm.call("place", {"window": wid, "grid": {"cols": 3, "rows": 2}, "selection": {"col0": 1, "row0": 0, "col1": 2, "row1": 0}})
        time.sleep(1)
        saved_frame = frame(wid)
        snap = (vm.call("snapshot_save", {"name": "before upgrade"}).get("result") or {}).get("snapshot_id")

        def kept(tag):
            c2 = cfg()
            lst = vm.call("snapshot_list", {"limit": 500}).get("result") or []
            names = [x.get("name") for x in lst if isinstance(x, dict) and x.get("name")]
            ok = (c2.get("appearance", {}).get("default_grid") == {"cols": 8, "rows": 5} and c2.get("general", {}).get("panel_shortcut") == "Ctrl+Alt+P"
                  and any(x.get("id") == "u-top-left" for x in c2.get("shortcuts", [])) and "before upgrade" in names)
            return ok, f"{tag}: grid {c2.get('appearance', {}).get('default_grid')} panel {c2.get('general', {}).get('panel_shortcut')} snapshots {names[:5]}"

        steps = old_debs[1:] + [deb]
        prev_ver = v0.split("-")[0]
        for i, path in enumerate(steps, 1):
            v = ver_of(path).split("-")[0]
            pid0 = status().get("pid")
            out = install(path, f"step{i}")
            check(f"U1{i}a", f"{prev_ver} -> {v}: the package upgrades over the running install", "rc=0" in out.stdout and f"\t{v}-" in out.stdout,
                  " ".join(out.stdout.split())[-250:])
            # Self-restart in place (same pid) when the running build has it; otherwise the user logs out and in.
            restarted = wait_for(lambda: status().get("version") == v, 150, 5)
            how = f"restarted itself in place (pid {pid0} -> {status().get('pid')})" if restarted else ""
            if not restarted:
                relogin()
                restarted = status().get("version") == v
                how = "after a log-out and log-in (this build has no self-restart; the package said to restart the service)"
            n = vm.ssh("pgrep -u tester -c -x fractiled").stdout.strip()
            ws = wait_session(vm, stype, timeout=120)
            check(f"U1{i}b", f"{prev_ver} -> {v}: the new service runs, once, and controls windows",
                  restarted and n == "1" and (ws.get("status") or {}).get("window_control"), f"{how}; fractiled processes {n}")
            ok, d = kept(v)
            check(f"U1{i}c", f"{prev_ver} -> {v}: settings, the user's shortcut and the snapshot are kept", ok, d)
            prev_ver = v

        # The old snapshot restores with the new build.
        def editor():
            return next((w for w in (vm.call("list_windows").get("result") or []) if w.get("desktop_file_id") == "org.gnome.TextEditor.desktop"), None)

        ed2 = editor()
        if not ed2:
            # the log-out after the first upgrade closed it
            vm.vmctl("launch org.gnome.TextEditor.desktop")
            ed2 = wait_for(editor, 60, 2)
        if ed2:
            vm.call("place", {"window": ed2["id"], "grid": {"cols": 2, "rows": 2}, "selection": {"col0": 0, "row0": 1, "col1": 0, "row1": 1}})
            time.sleep(1)
            r = vm.call("restore_start", {"snapshot": snap, "wait": True}).get("result") or {}
            got = wait_for(lambda: (lambda f: f if f and saved_frame and all(abs(f[k] - saved_frame[k]) <= 4 for k in "xywh") else None)(frame(ed2["id"])), 30, 1) or frame(ed2["id"])
            check("U20", "the snapshot saved by the oldest build restores with the new build", bool(got) and all(abs(got[k] - saved_frame[k]) <= 4 for k in "xywh"),
                  f"saved {saved_frame} got {got}; run {json.dumps(r)[:200]}")
        else:
            check("U20", "the snapshot saved by the oldest build restores with the new build", False, "no editor window after the upgrade")
        check("U21", "the user's global shortcuts are registered by the new build", not (vm.vmctl("session").get("status") or {}).get("hotkey_errors"),
              json.dumps((vm.vmctl("session").get("status") or {}).get("hotkey_errors"))[:200])

        # A reboot after the upgrade: one service, same settings.
        vm.ssh("sudo systemctl reboot", timeout=20)
        vm.wait_down()
        vm.wait_ssh(600)
        ws = wait_session(vm, stype)
        n = vm.ssh("pgrep -u tester -c -x fractiled").stdout.strip()
        ok, d = kept("after reboot")
        check("U30", "after a reboot one new service starts from the unit, with the settings and snapshot kept",
              status().get("version") == prev_ver and n == "1" and (ws.get("status") or {}).get("window_control") and ok, f"{d}; processes {n}")
        dup = vm.ssh("ls ~/.config/autostart/ /etc/xdg/autostart/ 2>/dev/null | grep -ci fractile").stdout.strip()
        check("U31", "no duplicate autostart entries after the upgrades", dup in ("0", "1"), f"{dup} autostart entries")
    finally:
        if HOLD and HOLD_AT in ("end", "fail"):
            say(f"holding the VM for {HOLD} s (create /tmp/release in the container to go on)")
            wait_for(lambda: Path("/tmp/release").exists(), HOLD, 2)
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

        # DV-19 / DV-02: the tray icon in the desktop's own tray (Plasma's, or
        # on Ubuntu GNOME the AppIndicator extension Ubuntu enables by default)
        if True:
            vm.ssh("XDG_RUNTIME_DIR=/run/user/$(id -u) fractile config set general.tray_icon true")
            tr = vm.vmctl("tray")
            shot = vm.screenshot(f"panel-{session}-18-tray")
            if not check("P18", "with the tray icon on, Fractile's icon is registered in the desktop's tray (StatusNotifierWatcher) and active",
                         tr.get("registered") and "Active" in (tr.get("status") or ""), f"{json.dumps(tr)[:300]}; {shot}"):
                hold("tray")
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
    ap.add_argument("--upgrade-from", nargs="+", help="PK-06: earlier .debs (oldest first) installed before --deb")
    ap.add_argument("--hold", type=int, default=0, help="debug: keep the VM up this many seconds after the first login")
    ap.add_argument("--hold-at", default="login", choices=["login", "after-reboot", "end", "fail", "tray"])
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
        if a.upgrade_from:
            run_upgrade(a.session, a.upgrade_from, a.deb)
        else:
            if a.session == "cosmic":
                run_cosmic(a.deb)
            else:
                (run_panel if PANEL else run)(a.session, a.deb)
    except Exception as ex:  # noqa: BLE001  record, then still write evidence
        import traceback
        traceback.print_exc()
        check("VXX", "run crashed", False, repr(ex))
    ver = RELEASES[SESSION_RELEASE[a.session]] if a.session else "24.04"
    distro = "vm-arch" if SESSION_RELEASE.get(a.session) == "arch" else f"vm-ubuntu{ver}"
    suite = f"{distro}-{a.session}" + ("-power-cut" if POWER_CUT else "") + ("-panel" if PANEL else "") + ("-upgrade" if a.upgrade_from else "")
    ev = {"suite": suite, "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": ((f"Arch Linux VM (QEMU/KVM, 4 vCPU, 6 GiB, virtio-vga 1600x1200, software rendering through llvmpipe), GDM timed login, COSMIC session "
                          f"(cosmic-session from the Arch repositories, auto-tiling on), Fractile {Path(a.deb).name} installed with pacman -U")
          if a.session == "cosmic" else ("Arch Linux VM (QEMU/KVM, 4 vCPU, 6 GiB, virtio-vga 1600x1200, software rendering through llvmpipe), GDM timed login, "
                          f"hyprland session (hyprlock via hypridle, waybar), Fractile {Path(a.deb).name} installed with pacman -U"))
          if SESSION_RELEASE.get(a.session) == "arch" else f"Ubuntu {ver} desktop VM (QEMU/KVM, 4 vCPU, 6 GiB, virtio-vga 1600x1200, software rendering){' with kde-plasma-desktop (Plasma 5.27)' if a.session == 'plasma-x11' else ' with kde-plasma-desktop (Plasma 6)' if a.session == 'plasma-wayland' else ''}, GDM timed login, {a.session} session, Fractile {Path(a.deb).name} installed with apt",
          "results": results, "log": log_lines}
    out = EVID / f"{suite}.json"
    out.write_text(json.dumps(ev, indent=2) + "\n")
    print(f"{sum(r['ok'] for r in results)}/{len(results)} checks passed; evidence: {out}")
    return 0 if results and all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
