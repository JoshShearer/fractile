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
  vmctl.py call METHOD [JSON]      one IPC call to the service; prints its result
  vmctl.py panel-prep              panel tests: the UI reports its grid geometry
                                   (FRACTILE_UI_TEST_GEOMETRY), panel shortcut
                                   Ctrl+Alt+G, service restarted with that
  vmctl.py panel-geom              where the open panel's grid is on screen
  vmctl.py tray                    Fractile's item in the desktop's tray
                                   (StatusNotifierWatcher) and its properties
  vmctl.py tray-activate           what a left click on the tray icon sends
                                   (StatusNotifierItem.Activate)
  vmctl.py settings-probe          tests/gui/settings_probe.py against the
                                   installed UI, keys from a uinput keyboard
"""
from __future__ import annotations

import json
import os
import shutil
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
PLASMA = "KDE" in ENV.get("XDG_CURRENT_DESKTOP", "").split(":")
SHELL = "plasmashell" if PLASMA else "gnome-shell"


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
    return run("pgrep", "-u", str(UID), "-x", SHELL).returncode == 0


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
    pids = run("pgrep", "-u", str(UID), "-x", SHELL).stdout.split()
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
    if PLASMA and ENV.get("XDG_SESSION_TYPE") == "wayland":
        # Plasma Wayland: the KWin script, enabled the way System Settings >
        # Window Management > KWin Scripts does it (kwinrc [Plugins], then
        # KWin reconfigures and loads it). 0 when KWin reports it loaded.
        w = run("kwriteconfig6", "--file", "kwinrc", "--group", "Plugins", "--key", "fractileEnabled", "true")
        r = run("dbus-send", "--session", "--print-reply", "--dest=org.kde.KWin", "/KWin", "org.kde.KWin.reconfigure")
        loaded = wait_for(lambda: "boolean true" in run("dbus-send", "--session", "--print-reply", "--dest=org.kde.KWin", "/Scripting",
                                                          "org.kde.kwin.Scripting.isScriptLoaded", "string:fractile").stdout, 20.0)
        out["extension"] = 0 if (w.returncode == 0 and r.returncode == 0 and loaded) else 1
        out["extension_info"] = [f"kwriteconfig6 {w.returncode}", f"reconfigure {r.returncode} {r.stderr.strip()[-200:]}", f"script loaded {bool(loaded)}"]
    elif PLASMA:
        # Plasma X11 needs no integration: the service talks to the X server.
        out["extension"] = "n/a"
    else:
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


def new_window_action(path: str) -> tuple[str, str | None, bool] | None:
    """(action id, its Exec, DBusActivatable) of a desktop file's new-window
    action, if it declares one."""
    group, acts, execs, dbus = None, [], {}, False
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if line.startswith("["):
            group = line
        elif group == "[Desktop Entry]" and line.startswith("Actions="):
            acts = [a for a in line[8:].split(";") if a]
        elif group == "[Desktop Entry]" and line == "DBusActivatable=true":
            dbus = True
        elif group and group.startswith("[Desktop Action ") and line.startswith("Exec="):
            execs.setdefault(group[16:-1], line[5:])
    a = next((a for a in acts if a.replace("-", "").replace("_", "").lower() == "newwindow"), None)
    return (a, execs.get(a), dbus) if a else None


def launch(desktop_id: str, another: bool = False) -> int:
    """Start an app the way the app menu does: its desktop entry, in a
    scope of its own under the user manager. (Running gtk-launch as a
    transient systemd-run service is wrong: when gtk-launch exits, systemd
    stops the unit and kills the app it just started.)"""
    path = f"/usr/share/applications/{desktop_id}"
    # Plasma's menu starts apps through KIO (D-Bus activation for
    # DBusActivatable entries, waited for), GNOME Shell through GLib in its
    # own process. `gio launch` is not Plasma's way, and on Ubuntu 26.04
    # (GLib 2.88) it loses the activation of D-Bus activatable apps: it exits
    # before dbus-daemon has started the app, which then never gets the call.
    kio = shutil.which("kioclient") or shutil.which("kioclient5") or "kioclient"
    launcher = [kio, "exec", path] if PLASMA else ["gio", "launch", path]
    nw = new_window_action(path) if (another and PLASMA) else None
    if nw:
        # Another window of a running app: the taskbar's "New Window" (the
        # entry's new-window action); activating it again would only raise
        # the window it has (Files 50 does that).
        aid, aexec, dbus = nw
        stem = desktop_id[:-len(".desktop")]
        if dbus:
            launcher = ["gdbus", "call", "--session", "--dest", stem, "--object-path", "/" + stem.replace(".", "/").replace("-", "_"),
                        "--method", "org.freedesktop.Application.ActivateAction", f"'{aid}'", "[]", "{}"]
        elif aexec:
            launcher = [a for a in aexec.split() if not a.startswith("%")]
    # No pipes: the app inherits them, and waiting for EOF would wait for it.
    return subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", *launcher], env=ENV,
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
            launch(app, another=before > 0)
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
    if PLASMA:
        # What "Log Out" in the Plasma menu does, without its confirmation:
        # org.kde.Shutdown (Plasma 5.25+), else ksmserver itself.
        p = run("dbus-send", "--session", "--print-reply", "--dest=org.kde.Shutdown", "/Shutdown", "org.kde.Shutdown.logout")
        if p.returncode != 0:
            p = run("dbus-send", "--session", "--print-reply", "--dest=org.kde.ksmserver", "/KSMServer",
                    "org.kde.KSMServerInterface.logout", "int32:0", "int32:0", "int32:0")
        return {"rc": p.returncode, "stderr": p.stderr.strip()}
    p = run("gnome-session-quit", "--logout", "--no-prompt")
    return {"rc": p.returncode, "stderr": p.stderr.strip()}


GEOM = f"{RUNTIME}/fractile-panel-geometry.json"


def cmd_call() -> dict:
    from fractile_ui.ipc import Client, RpcError
    c = Client("vm-test")
    try:
        return {"result": c.call(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {})}
    except RpcError as ex:
        return {"error": str(ex)}
    finally:
        c.close()


def cmd_panel_prep() -> dict:
    out = {}
    out["set_env"] = run("systemctl", "--user", "set-environment", f"FRACTILE_UI_TEST_GEOMETRY={GEOM}").returncode
    out["restart"] = run("systemctl", "--user", "restart", "fractile.service", timeout=60).returncode
    # a UI started before carries the old environment
    run("pkill", "-u", str(UID), "-f", "fractile-ui")
    up = wait_for(lambda: run("fractile", "status").returncode == 0, 30.0)
    out["service_up"] = bool(up)
    out["shortcut"] = run("fractile", "config", "set", "general.panel_shortcut", '"Ctrl+Alt+G"').returncode
    out["first_run"] = run("fractile", "config", "set", "general.first_run_complete", "true").returncode
    # the compositor integration reconnects to the restarted service
    wait_for(lambda: (try_cli("status") or {}).get("window_control"), 30.0)
    st = try_cli("status") or {}
    out["status"] = {k: st.get(k) for k in ("adapter", "session_type", "window_control", "global_shortcuts", "hotkey_errors", "ui_attached")}
    return out


def panel_windows():
    return [w for w in (try_cli("list-windows", "--all") or []) if isinstance(w, dict) and (w.get("title") or "").startswith("Fractile panel")
            and not (w.get("state") or {}).get("minimized")]


def cmd_x11_preview() -> dict:
    """The mapped "Fractile preview" window on X11 (override-redirect, so only
    the X server knows it), from xwininfo."""
    import re
    p = run("xwininfo", "-root", "-tree")
    for line in p.stdout.splitlines():
        if '"Fractile preview"' in line:
            m = re.search(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s+\+(-?\d+)\+(-?\d+)", line)
            if not m:
                continue
            w, h, _, _, x, y = map(int, m.groups())
            st = run("xwininfo", "-id", line.split()[0])
            if "IsViewable" in st.stdout:
                return {"frame": {"x": x, "y": y, "w": w, "h": h}}
    return {"frame": None, "rc": p.returncode, "err": p.stderr[-200:]}


def cmd_panel_geom() -> dict:
    """The grid of each open panel in screen coordinates: the panel window's
    frame (from the compositor) plus the grid's offset inside it (from the UI;
    a Wayland client does not know its own screen position)."""
    try:
        g = json.load(open(GEOM))
    except (OSError, ValueError):
        return {"grids": [], "why": "no geometry file"}
    wins = panel_windows()
    grids = []
    for d in g:
        w = next((w for w in wins if w.get("output_id") == d.get("output")), wins[0] if len(wins) == 1 else None)
        if w:
            grids.append(dict(d, x=w["frame"]["x"] + d["ox"], y=w["frame"]["y"] + d["oy"], panel=w["frame"], panel_id=w["id"]))
    return {"grids": grids, "mtime": os.path.getmtime(GEOM)}


def _gdbus(*args) -> str:
    return run("gdbus", "call", "--session", *args).stdout.strip()


def tray_item():
    """(service, path) of Fractile's StatusNotifierItem, or None."""
    out = run("gdbus", "call", "--session", "--dest", "org.kde.StatusNotifierWatcher", "--object-path", "/StatusNotifierWatcher",
              "--method", "org.freedesktop.DBus.Properties.Get", "org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems").stdout
    import re
    for item in re.findall(r"'([^']+)'", out):
        svc, _, path = item.partition("/")
        path = "/" + path if path else "/StatusNotifierItem"
        title = _gdbus("--dest", svc, "--object-path", path, "--method", "org.freedesktop.DBus.Properties.Get", "org.kde.StatusNotifierItem", "Title")
        ident = _gdbus("--dest", svc, "--object-path", path, "--method", "org.freedesktop.DBus.Properties.Get", "org.kde.StatusNotifierItem", "Id")
        if "Fractile" in title or "fractile" in ident.lower():
            return svc, path, title, ident
    return None


def cmd_tray() -> dict:
    it = wait_for(tray_item, 20.0)
    if not it:
        return {"registered": False, "watcher": run("gdbus", "introspect", "--session", "--dest", "org.kde.StatusNotifierWatcher",
                                                    "--object-path", "/StatusNotifierWatcher").returncode == 0}
    svc, path, title, ident = it
    status = _gdbus("--dest", svc, "--object-path", path, "--method", "org.freedesktop.DBus.Properties.Get", "org.kde.StatusNotifierItem", "Status")
    return {"registered": True, "service": svc, "path": path, "title": title, "id": ident, "status": status}


def cmd_tray_activate() -> dict:
    it = tray_item()
    if not it:
        return {"activated": False}
    svc, path = it[0], it[1]
    p = run("gdbus", "call", "--session", "--dest", svc, "--object-path", path, "--method", "org.kde.StatusNotifierItem.Activate", "800", "1180")
    return {"activated": p.returncode == 0, "stderr": p.stderr.strip()[-200:]}


def cmd_settings_probe() -> dict:
    env = dict(ENV, PYTHONPATH="/usr/lib/fractile", PYTHONDONTWRITEBYTECODE="1",
               FRACTILE_TEST_KEYS_CMD="sudo -n python3 /home/tester/t/tests/tools/uinput_keys.py",
               QT_QPA_PLATFORM="wayland" if ENV.get("XDG_SESSION_TYPE") == "wayland" else "xcb")
    if PLASMA and ENV.get("XDG_SESSION_TYPE") != "wayland":
        env["FRACTILE_PROBE_X11_HELD"] = "ctrl+alt+delete"
    os.makedirs("/tmp/shots", exist_ok=True)
    p = subprocess.run(["python3", "/home/tester/t/tests/gui/settings_probe.py", "/tmp/shots"], env=env, capture_output=True, text=True, timeout=600)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"results": [{"id": "SXX", "name": "probe produced results", "ok": False, "detail": (p.stdout + p.stderr)[-800:]}]}


def main() -> int:
    cmd = sys.argv[1]
    fn = {"session": cmd_session, "setup": cmd_setup, "open-apps": cmd_open_apps, "state": cmd_state, "logout": cmd_logout, "launch": cmd_launch,
          "call": cmd_call, "panel-prep": cmd_panel_prep, "panel-geom": cmd_panel_geom,
          "tray": cmd_tray, "x11-preview": cmd_x11_preview, "tray-activate": cmd_tray_activate, "settings-probe": cmd_settings_probe}[cmd]
    print(json.dumps(fn()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
