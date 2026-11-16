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
        if k in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "SWAYSOCK",
                 "HYPRLAND_INSTANCE_SIGNATURE"):
            env[k] = v
    # Hyprland started without uwsm need not export its environment to the
    # user manager; its own instance list names the instance and the Wayland
    # socket.
    if shutil.which("hyprctl") and subprocess.run(["pgrep", "-u", str(UID), "-x", "Hyprland"], capture_output=True).returncode == 0:
        try:
            inst = json.loads(subprocess.run(["hyprctl", "instances", "-j"], env=env, capture_output=True, text=True, timeout=10).stdout or "[]")
        except (ValueError, subprocess.TimeoutExpired):
            inst = []
        if inst:
            newest = max(inst, key=lambda i: i.get("time", 0))
            env.update(HYPRLAND_INSTANCE_SIGNATURE=newest["instance"], WAYLAND_DISPLAY=newest.get("wl_socket") or env.get("WAYLAND_DISPLAY", ""),
                       XDG_CURRENT_DESKTOP=env.get("XDG_CURRENT_DESKTOP") or "Hyprland", XDG_SESSION_TYPE="wayland")
    # COSMIC: if the session did not export its environment to the user
    # manager, its panel (a client of the compositor) has it.
    if not env.get("WAYLAND_DISPLAY"):
        for pid in subprocess.run(["pgrep", "-u", str(UID), "-x", "cosmic-panel"], capture_output=True, text=True).stdout.split()[:1]:
            try:
                penv = dict(kv.split("=", 1) for kv in open(f"/proc/{pid}/environ").read().split("\0") if "=" in kv)
            except OSError:
                penv = {}
            for k in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DISPLAY"):
                if penv.get(k) and not env.get(k):
                    env[k] = penv[k]
    return env


ENV = session_env()
PLASMA = "KDE" in ENV.get("XDG_CURRENT_DESKTOP", "").split(":")
SWAY = "sway" in ENV.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
HYPR = "hyprland" in ENV.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
COSMIC = "cosmic" in ENV.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
SHELL = "plasmashell" if PLASMA else "sway" if SWAY else "Hyprland" if HYPR else "cosmic-comp" if COSMIC else "gnome-shell"


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
    elif PLASMA or SWAY or HYPR or COSMIC:
        # Plasma X11 needs no integration (the service talks to the X
        # server), nor do Sway and Hyprland (their IPC sockets); COSMIC has
        # nothing to enable.
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
    if SWAY or HYPR:
        # Sway's launcher (wmenu-run) and `exec` bindings run the program
        # itself. D-Bus activation does not work there for these GNOME apps on
        # Ubuntu 26.04 (they exit without a window), so the Exec line, or for
        # another window the entry's new-window action, through `swaymsg exec`.
        nw = new_window_action(path) if another else None
        exe = (nw[1] if nw and nw[1] else None) or next((ln.strip()[5:] for ln in open(path, encoding="utf-8") if ln.startswith("Exec=")), "")
        argv = [a for a in exe.split() if not a.startswith("%")]
        if HYPR:
            # Hyprland's launchers and binds run the program through its
            # exec dispatcher (the Lua form of this configuration).
            return run("hyprctl", "dispatch", f"hl.exec_cmd({json.dumps(' '.join(argv))})").returncode
        return run("swaymsg", "exec", " ".join(argv)).returncode
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
    if SWAY:
        # Ubuntu's Sway session never reaches graphical-session.target, so the
        # user services that are PartOf it (the desktop portal) are not
        # stopped at log-out; normally they end with the user manager, and the
        # session bus with it. The test's own ssh connections keep both
        # running across the log-out. Then, when Sway exits, the old portal
        # re-activates its GTK backend, which cannot open a display and fails;
        # dbus-daemon keeps that activation pending for its 120 s timeout, and
        # every GTK app of the next login waits on it (seen on Ubuntu 26.04).
        # Stop the portal first, as the end of the user manager would.
        st = run("systemctl", "--user", "stop", "xdg-desktop-portal.service", "xdg-desktop-portal-gtk.service")
        # Sway's default "exit" binding asks first through swaynag; the
        # command itself is `swaymsg exit`.
        p = run("swaymsg", "exit")
        gone = wait_for(lambda: run("pgrep", "-u", str(UID), "-x", "sway").returncode != 0, 30.0, 0.2)
        return {"rc": p.returncode, "stderr": p.stderr.strip(), "sway_exited": bool(gone), "portal_stopped_first": st.returncode == 0}
    if HYPR:
        # As on Sway, nothing ends the portal at log-out without uwsm; stop it
        # first (see above). Then Hyprland's own exit dispatcher.
        st = run("systemctl", "--user", "stop", "xdg-desktop-portal.service", "xdg-desktop-portal-hyprland.service", "xdg-desktop-portal-gtk.service")
        p = run("hyprctl", "dispatch", "hl.dsp.exit()")
        gone = wait_for(lambda: run("pgrep", "-u", str(UID), "-x", "Hyprland").returncode != 0, 30.0, 0.2)
        return {"rc": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip(), "hyprland_exited": bool(gone), "portal_stopped_first": st.returncode == 0}
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
        # the UI names each panel after its monitor ("Fractile panel <output id>")
        w = next((w for w in wins if w.get("title") == f"Fractile panel {d.get('output')}"), None) \
            or next((w for w in wins if w.get("output_id") == d.get("output")), wins[0] if len(wins) == 1 else None)
        if w:
            grids.append(dict(d, x=w["frame"]["x"] + d["ox"], y=w["frame"]["y"] + d["oy"], panel=w["frame"], panel_id=w["id"]))
    return {"grids": grids, "mtime": os.path.getmtime(GEOM), "windows": [(w.get("title"), w.get("frame"), w.get("output_id")) for w in wins]}


def _gdbus(*args) -> str:
    return run("gdbus", "call", "--session", *args).stdout.strip()


def tray_item():
    """(service, path) of Fractile's StatusNotifierItem, or None."""
    out = run("gdbus", "call", "--session", "--dest", "org.kde.StatusNotifierWatcher", "--object-path", "/StatusNotifierWatcher",
              "--method", "org.freedesktop.DBus.Properties.Get", "org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems").stdout
    import re
    for item in re.findall(r"'([^']+)'", out):
        # KDE's watcher lists "<bus name>/<path>", the AppIndicator extension
        # (Ubuntu GNOME) "<bus name>@<path>", Qt registers a bare bus name
        svc, _, path = item.partition("/")
        svc = svc.rstrip("@")
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
    if ENV.get("XDG_SESSION_TYPE") != "wayland":
        # a combination the desktop itself binds (log out on GNOME and KDE)
        env["FRACTILE_PROBE_X11_HELD"] = "ctrl+alt+delete"
    os.makedirs("/tmp/shots", exist_ok=True)
    p = subprocess.run(["python3", "/home/tester/t/tests/gui/settings_probe.py", "/tmp/shots"], env=env, capture_output=True, text=True, timeout=600)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"results": [{"id": "SXX", "name": "probe produced results", "ok": False, "detail": (p.stdout + p.stderr)[-800:]}]}


def cosmic_config() -> dict:
    """sha256 of every file under ~/.config/cosmic: COSMIC's own settings,
    auto-tiling among them, which Fractile must never write (T18)."""
    import hashlib
    root = os.path.expanduser("~/.config/cosmic")
    out = {}
    for d, _subs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            try:
                out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
            except OSError as ex:
                out[os.path.relpath(p, root)] = f"unreadable: {ex}"
    return out


def cmd_cosmic_before() -> dict:
    """COSMIC (T17): what Fractile reports, before anything is asked of it;
    then a text editor window opened from its desktop entry."""
    out = {"status": try_cli("status"), "diagnose_rc": None, "config": cosmic_config(),
           "unit_active": run("systemctl", "--user", "is-active", "fractile.service").stdout.strip()}
    d = run("fractile", "diagnose")
    out["diagnose_rc"], out["diagnose"] = d.returncode, (d.stdout + d.stderr)[-3000:]
    t = run("fractile", "status")
    out["status_text"] = (t.stdout + t.stderr)[-2000:]
    up = lambda: run("pgrep", "-u", str(UID), "-x", "gnome-text-edit").stdout.strip()
    out["launch_rc"] = launch("org.gnome.TextEditor.desktop")
    out["editor"] = bool(wait_for(up, 30.0))
    if not out["editor"]:
        # Test set-up, not the product: `gio launch` of this D-Bus
        # activatable entry was once lost right after login (one run of
        # seven); a user would click the icon again.
        out["launch_retry_rc"] = launch("org.gnome.TextEditor.desktop")
        out["editor"] = bool(wait_for(up, 30.0))
    time.sleep(5)
    return out


EDITOR = "org.gnome.TextEditor.desktop"


def editors() -> list:
    return [w for w in (try_cli("list-windows") or []) if isinstance(w, dict) and w.get("desktop_file_id") == EDITOR]


def ipc(method: str, params: dict | None = None) -> dict:
    from fractile_ui.ipc import Client, RpcError
    c = Client("vm-test")
    try:
        return {"result": c.call(method, params or {})}
    except RpcError as ex:
        return {"error": str(ex)}
    finally:
        c.close()


def cmd_cosmic_try() -> dict:
    """What the COSMIC adapter can do (list windows, outputs and workspaces)
    and what it must refuse with the reason (placing, snapping): nothing may
    be moved or resized, nothing may claim to have been placed."""
    out = {}
    for name, args in (("list-windows", ("list-windows",)), ("list-outputs", ("list-outputs",)),
                       ("place", ("place", "--grid", "2x2", "--cells", "0,0,0,0")),
                       ("snap", ("snap", "left-half"))):
        p = run("fractile", "--json", *args)
        out[name] = {"rc": p.returncode, "stdout": p.stdout.strip()[-3000:], "stderr": p.stderr.strip()[-600:]}
        try:
            out[name]["json"] = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None
        except ValueError:
            out[name]["json"] = None
    out["workspaces"] = ipc("list_workspaces")
    out["editors"] = editors()
    out["snapshots"] = try_cli("list-snapshots", "--limit", "5")
    out["status"] = try_cli("status")
    out["config"] = cosmic_config()
    out["log"] = run("bash", "-c", "grep -E 'warn|error|unsupported|cosmic' ~/.local/state/fractile/fractile.log 2>/dev/null | tail -40").stdout[-4000:]
    return out


def cmd_cosmic_arrange() -> dict:
    """The user puts the text editor on the second workspace (index 1) and maximizes it (the
    service's own requests, which COSMIC's toplevel manager carries out),
    then saves a snapshot."""
    out = {"before": editors(), "config": cosmic_config()}
    ed = out["before"][0] if out["before"] else None
    if not ed:
        return out
    out["move"] = ipc("move_to_workspace", {"window": ed["id"], "workspace": 1})
    out["maximize"] = ipc("set_state", {"window": ed["id"], "state": {"maximized": True}})
    # On a workspace not in view COSMIC reports the new state about 25 s
    # late; the request may say so instead of confirming.
    t0 = time.time()
    wait_for(lambda: (lambda e: e and e[0].get("workspace") == 1 and e[0]["state"].get("maximized"))(editors()), 60.0, 1.0)
    out["confirmed_after_s"] = round(time.time() - t0, 1)
    time.sleep(2)
    out["after"] = editors()
    out["save"] = try_cli("save", "--name", "cosmic-check")
    sid = (out["save"] or {}).get("snapshot_id") if isinstance(out["save"], dict) else None
    snap = (ipc("snapshot_get", {"id": sid}).get("result") if sid else None) or {}
    snap = snap.get("snapshot", snap) if isinstance(snap, dict) else {}
    out["snapshot_id"] = sid
    out["records"] = [{"app": r.get("desktop_file_id"), "workspace_id": r.get("workspace_id"), "state": r.get("state"), "frame": r.get("absolute_geometry")}
                      for r in (snap.get("windows") or [])]
    out["workspaces"] = snap.get("workspaces")
    out["config"] = cosmic_config()
    return out


def cmd_cosmic_after() -> dict:
    """After the next login: the editor windows, and the last restore run
    with each item's status and message."""
    out = {"editors": editors(), "restore": try_cli("restore-status")}
    runs = (out["restore"] or {}).get("recent_runs") or [] if isinstance(out["restore"], dict) else []
    rid = runs[0].get("run_id") if runs else None
    out["run"] = ipc("restore_run", {"run_id": rid}).get("result") if rid else None
    # What the run restored from: the editor's record in that snapshot.
    src = (out["run"] or {}).get("source_id")
    snap = (ipc("snapshot_get", {"id": src}).get("result") if src else None) or {}
    out["source_records"] = [{"workspace_id": r.get("workspace_id"), "state": r.get("state"), "mode": snap.get("mode")}
                             for r in (snap.get("windows") or []) if r.get("desktop_file_id") == EDITOR]
    out["snapshots"] = try_cli("list-snapshots", "--limit", "5")
    out["status"] = try_cli("status")
    out["procs"] = len(run("pgrep", "-u", str(UID), "-x", "gnome-text-edit").stdout.split())
    return out


def cmd_cosmic_restore_again() -> dict:
    """Restore the same snapshot once more by hand: nothing new may open."""
    sid = sys.argv[2]
    r = ipc("restore_start", {"snapshot": sid, "wait": True})
    time.sleep(3)
    return {"restore": r, "editors": editors(), "procs": len(run("pgrep", "-u", str(UID), "-x", "gnome-text-edit").stdout.split())}


def cmd_kscreen() -> dict:
    """kscreen-doctor in the session (Plasma's own display settings tool),
    e.g. `kscreen output.Virtual-2.rotation.left`."""
    if not shutil.which("kscreen-doctor"):
        return {"rc": 127, "stderr": "kscreen-doctor is not installed"}
    p = run("kscreen-doctor", *sys.argv[2:], timeout=60)
    return {"rc": p.returncode, "stdout": p.stdout[-1500:], "stderr": p.stderr[-800:]}


def cmd_exec() -> dict:
    """Run an argv (JSON list) in the desktop session, in its own scope, as a
    launcher would (for apps started with test-profile arguments)."""
    # A scope runs the program in the foreground: do not wait for it (a
    # browser stays in the foreground; waiting timed out and killed it).
    argv = json.loads(sys.argv[2])
    p = subprocess.Popen(["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", *argv], env=ENV,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    return {"rc": p.poll(), "pid": p.pid}


def main() -> int:
    cmd = sys.argv[1]
    fn = {"session": cmd_session, "setup": cmd_setup, "open-apps": cmd_open_apps, "state": cmd_state, "logout": cmd_logout, "launch": cmd_launch,
          "call": cmd_call, "panel-prep": cmd_panel_prep, "panel-geom": cmd_panel_geom,
          "tray": cmd_tray, "x11-preview": cmd_x11_preview, "tray-activate": cmd_tray_activate, "settings-probe": cmd_settings_probe,
          "cosmic-before": cmd_cosmic_before, "cosmic-try": cmd_cosmic_try,
          "cosmic-arrange": cmd_cosmic_arrange, "cosmic-after": cmd_cosmic_after, "cosmic-restore-again": cmd_cosmic_restore_again, "kscreen": cmd_kscreen, "exec": cmd_exec}[cmd]
    print(json.dumps(fn()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
