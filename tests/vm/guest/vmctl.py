#!/usr/bin/env python3
"""Runs inside the test VM as the desktop user (over ssh). Test tool only.

Talks to the installed Fractile through its CLI and its own IPC client
(/usr/lib/fractile/fractile_ui/ipc.py), and to the desktop session through
the systemd user manager's environment. Prints one JSON object per command.

  vmctl.py session                 who is logged in, session type, Fractile status
  vmctl.py setup                   what the first-run wizard and Settings would
                                   set: restore policy, autostart, per-app
                                   launch approval, extension enabled
  vmctl.py open-apps               open the test apps, place every window on a
                                   grid cell, save a snapshot; print the layout
  vmctl.py state                   windows, outputs, restore status
  vmctl.py logout                  log out of the desktop the way the menu does
  vmctl.py launch ID.desktop       start an app the way the app menu does
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, "/usr/lib/fractile")

UID = os.getuid()
RUNTIME = f"/run/user/{UID}"

# 10 applications, 15 windows. Terminal and Files open a new window each time
# they are started; the others are single-window apps.
APPS = [
    ("org.gnome.Terminal.desktop", 4),
    ("org.gnome.Nautilus.desktop", 3),
    ("org.gnome.TextEditor.desktop", 1),
    ("org.gnome.Calculator.desktop", 1),
    ("org.gnome.SystemMonitor.desktop", 1),
    ("org.gnome.Characters.desktop", 1),
    ("org.gnome.clocks.desktop", 1),
    ("org.gnome.baobab.desktop", 1),
    ("org.gnome.DiskUtility.desktop", 1),
    ("org.gnome.font-viewer.desktop", 1),
]


def session_env() -> dict:
    env = dict(os.environ, XDG_RUNTIME_DIR=RUNTIME, DBUS_SESSION_BUS_ADDRESS=f"unix:path={RUNTIME}/bus")
    p = subprocess.run(["systemctl", "--user", "show-environment"], env=env, capture_output=True, text=True)
    for line in p.stdout.splitlines():
        k, _, v = line.partition("=")
        if k in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
            env[k] = v
    return env


ENV = session_env()


def run(*args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), env=ENV, capture_output=True, text=True, timeout=timeout)


def cli(*args, timeout=120):
    p = run("fractile", "--json", *args, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"fractile {' '.join(args)} -> {p.returncode}: {p.stderr.strip()}")
    return json.loads(p.stdout) if p.stdout.strip() else None


def try_cli(*args):
    try:
        return cli(*args)
    except Exception as ex:  # noqa: BLE001  report, do not crash
        return {"error": str(ex)}


def wait_for(fn, timeout, step=0.5):
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


def shell_running() -> bool:
    return run("pgrep", "-u", str(UID), "-x", "gnome-shell").returncode == 0


def graphical_session() -> str | None:
    """The type (wayland/x11) of this user's active local graphical logind
    session, or None. ssh sessions are type tty and do not count."""
    for line in run("loginctl", "list-sessions", "--no-legend").stdout.splitlines():
        sid = line.split()[0] if line.split() else ""
        props = dict(kv.split("=", 1) for kv in run("loginctl", "show-session", sid, "-p", "Type", "-p", "State", "-p", "Remote", "-p", "Name", "-p", "Class").stdout.split() if "=" in kv)
        if props.get("Name") == os.environ.get("USER", "tester") and props.get("Class") == "user" and props.get("Remote") == "no" \
                and props.get("State") == "active" and props.get("Type") in ("wayland", "x11"):
            return props["Type"]
    return None


def cmd_session() -> dict:
    st = try_cli("status")
    pids = run("pgrep", "-u", str(UID), "-x", "gnome-shell").stdout.split()
    loginctl = run("loginctl", "list-sessions", "--no-legend").stdout.strip()
    return {
        "shell_running": bool(pids),
        "shell_pid": pids[0] if len(pids) == 1 else None,
        "graphical_session": graphical_session(),
        "session_type": ENV.get("XDG_SESSION_TYPE"),
        "desktop": ENV.get("XDG_CURRENT_DESKTOP"),
        "display": ENV.get("WAYLAND_DISPLAY") or ENV.get("DISPLAY"),
        "loginctl": loginctl,
        "unit_active": run("systemctl", "--user", "is-active", "fractile.service").stdout.strip(),
        "unit_enabled": run("systemctl", "--user", "is-enabled", "fractile.service").stdout.strip(),
        "status": st,
        "boot_id": open("/proc/sys/kernel/random/boot_id").read().strip(),
    }


def cmd_setup() -> dict:
    from fractile_ui.ipc import Client
    out = {}
    # The extension: what the docs tell the user to do (or the Extensions app).
    out["extension"] = run("gnome-extensions", "enable", "fractile@fractile.io").returncode
    out["extension_info"] = run("gnome-extensions", "info", "fractile@fractile.io").stdout.strip().splitlines()[-3:]
    # What the first-run wizard writes on Finish, and the Applications page.
    c = Client("vm-setup")
    try:
        cfg = c.call("get_config")["config"]
        cfg["general"]["first_run_complete"] = True
        cfg["general"]["autostart"] = True
        cfg["session"]["policy"] = "last_clean"
        c.call("set_config", {"config": cfg})
        for app, _n in APPS:
            c.call("app_policy_set", {"desktop_id": app, "policy": {"launch": "if_missing", "extra_args": []}})
    finally:
        c.close()
    out["autostart"] = try_cli("autostart", "status")
    out["unit_enabled"] = run("systemctl", "--user", "is-enabled", "fractile.service").stdout.strip()
    out["policy"] = try_cli("config", "get", "session.policy")
    return out


def launch(desktop_id: str) -> int:
    """Start an app the way the GNOME app menu does: its desktop entry, in a
    scope of its own under the user manager. (Running gtk-launch as a
    transient systemd-run service is wrong: when gtk-launch exits, systemd
    stops the unit and kills the app it just started.)"""
    path = f"/usr/share/applications/{desktop_id}"
    # No pipes: the app inherits them, and waiting for EOF would wait for it.
    return subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", "gio", "launch", path], env=ENV,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60).returncode


def cmd_launch() -> dict:
    return {"rc": launch(sys.argv[2])}


def mine(ws):
    ids = {a for a, _ in APPS}
    return [w for w in ws if w.get("desktop_file_id") in ids]


def cmd_open_apps() -> dict:
    out = cli("list-outputs")
    o = out[0]
    for app, n in APPS:
        for _ in range(n):
            before = len([w for w in mine(cli("list-windows")) if w["desktop_file_id"] == app])
            launch(app)
            wait_for(lambda: len([w for w in mine(cli("list-windows")) if w["desktop_file_id"] == app]) > before, 30.0)
    ws = wait_for(lambda: (lambda l: l if len(l) == sum(n for _, n in APPS) else None)(mine(cli("list-windows"))), 30.0) or mine(cli("list-windows"))
    # One grid cell each on a 4x4 grid, in a fixed order.
    ws.sort(key=lambda w: (w["desktop_file_id"], w["id"]))
    placed = []
    for k, w in enumerate(ws):
        c, r = k % 4, k // 4
        res = cli("place", "--grid", "4x4", "--cells", f"{c},{r},{c},{r}", "--window", w["id"], "--output", o["stable_id"])
        placed.append({"id": w["id"], "app": w["desktop_file_id"], "cell": [c, r], "requested": res.get("requested"), "observed": res.get("observed")})
    time.sleep(2)
    final = {w["id"]: w["frame"] for w in cli("list-windows")}
    for p in placed:
        p["frame"] = final.get(p["id"])
    snap = try_cli("save", "--name", "before-reboot")
    return {"output": o["stable_id"], "work_area": o["work_area"], "windows": placed, "count": len(placed), "saved": snap}


def cmd_state() -> dict:
    return {
        "windows": [{"id": w["id"], "app": w.get("desktop_file_id"), "title": w.get("title"), "frame": w.get("frame")} for w in (try_cli("list-windows") or []) if isinstance(w, dict)],
        "restore": try_cli("restore-status"),
        "snapshots": try_cli("list-snapshots", "--limit", "5"),
        "status": try_cli("status"),
        "boot_id": open("/proc/sys/kernel/random/boot_id").read().strip(),
    }


def cmd_logout() -> dict:
    p = run("gnome-session-quit", "--logout", "--no-prompt")
    return {"rc": p.returncode, "stderr": p.stderr.strip()}


def main() -> int:
    cmd = sys.argv[1]
    fn = {"session": cmd_session, "setup": cmd_setup, "open-apps": cmd_open_apps, "state": cmd_state, "logout": cmd_logout, "launch": cmd_launch}[cmd]
    print(json.dumps(fn()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
