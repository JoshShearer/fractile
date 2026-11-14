#!/usr/bin/env python3
"""End-to-end tests on KWin Wayland through the Fractile KWin script.

Runs *inside* the test container (tests/kwin/e2e-kwin-in-container.sh): a
private D-Bus session, kwin_wayland --virtual (no Xwayland), the in-tree
KWin script loaded through KWin's Scripting D-Bus API, an isolated
fractiled, native Wayland GTK4/Qt test windows and the real Fractile UI.

Global shortcuts are triggered through KGlobalAccel's invokeShortcut D-Bus
method (the container has no input devices); this exercises Fractile's
registration with KDE and KDE's activation signal, not physical key
decoding. Fractile itself never injects input.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("FRACTILE_SRC", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT / "ui"))
from fractile_ui.ipc import Client  # noqa: E402

BIN = Path(os.environ["FRACTILE_BIN_DIR"])
TESTWIN = ROOT / "tests" / "tools" / "testwin.py"
SCRIPT = ROOT / "adapters" / "kwin" / "fractile" / "contents" / "code" / "main.js"
results: list[dict] = []


def check(tid, name, ok, detail=""):
    ok = bool(ok)
    results.append({"id": tid, "name": name, "ok": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {tid} {name}" + (f" :: {detail}" if detail else ""), flush=True)
    return ok


def rect_eq(a, b, tol=0):
    return bool(a and b and all(abs(a[k] - b[k]) <= tol for k in ("x", "y", "w", "h")))


def dbus(*args, check=True):
    p = subprocess.run(["dbus-send", "--session", "--print-reply", *args], capture_output=True, text=True, timeout=20)
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p.stdout + p.stderr


class Env:
    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="fractile-kwin-"))
        self.env = dict(os.environ)
        self.env.update(
            XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="KDE",
            FRACTILE_DATA_DIR=str(self.base / "data"), FRACTILE_CONFIG=str(self.base / "config.toml"),
            XDG_STATE_HOME=str(self.base / "state"), XDG_DATA_HOME=str(self.base / "xdgdata"),
            FRACTILE_NO_LOGIND="1", FRACTILE_AUTOSTART_MODE="none", FRACTILE_NO_SCOPE="1",
            QT_QPA_PLATFORM="wayland", GDK_BACKEND="wayland", GSETTINGS_BACKEND="memory",
        )
        self.sock = Path(self.env["XDG_RUNTIME_DIR"]) / "fractile" / "fractile.sock"
        self.ui_log = self.base / "ui.log"
        wrap = self.base / "ui-wrap.sh"
        wrap.write_text(f'#!/bin/sh\nexec python3 {ROOT / "ui" / "fractile-ui"} "$@" >>{self.ui_log} 2>&1\n')
        wrap.chmod(0o755)
        self.env["FRACTILE_UI"] = str(wrap)
        self.daemon = None
        self.procs = []

    def run(self, *a):
        return subprocess.run([str(BIN / "fractile"), *a], env=self.env, capture_output=True, text=True, timeout=120)

    def cli_json(self, *a):
        p = self.run("--json", *a)
        if p.returncode:
            raise RuntimeError(f"fractile {' '.join(a)} -> {p.returncode}: {p.stderr}")
        return json.loads(p.stdout)

    def client(self):
        return Client("e2e-kwin", self.sock)

    def call(self, m, p=None):
        c = self.client()
        try:
            return c.call(m, p or {})
        finally:
            c.close()

    def start_daemon(self):
        self.daemon = subprocess.Popen([str(BIN / "fractiled"), "--verbose"], env=self.env, stdout=open(self.base / "daemon.out", "a"), stderr=subprocess.STDOUT)
        for _ in range(100):
            if self.run("status").returncode == 0:
                return
            time.sleep(0.1)
        raise RuntimeError("daemon did not start")

    def stop_daemon(self):
        if self.daemon and self.daemon.poll() is None:
            self.daemon.terminate()
            self.daemon.wait(10)

    def window(self, tk, app, title):
        self.procs.append(subprocess.Popen([sys.executable, str(TESTWIN), "--toolkit", tk, "--app-id", app, "--title", title], env=self.env,
                                           stdout=subprocess.DEVNULL, stderr=open(self.base / f"win-{app}.log", "a")))

    def wait_for(self, fn, timeout=8.0, step=0.1):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(step)
        return fn()

    def windows(self, all_=False):
        return self.cli_json("list-windows", *(["--all"] if all_ else []))

    def frame(self, wid):
        return next((w["frame"] for w in self.windows(True) if w["id"] == wid), None)

    def status(self):
        return self.cli_json("status")


def load_script():
    out = dbus("--dest=org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript", f"string:{SCRIPT}", "string:fractile")
    dbus("--dest=org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.start")
    return out


def unload_script():
    dbus("--dest=org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", "string:fractile")


def invoke_shortcut(action):
    dbus("--dest=org.kde.kglobalaccel", "/component/fractile", "org.kde.kglobalaccel.Component.invokeShortcut", f"string:{action}")


def run_all(e: Env):
    e.start_daemon()
    st = e.status()
    check("K00", "before the script loads, Fractile reports no window control and says why",
          not st["window_control"] and (st.get("integration") or {}).get("connected") is False, (st.get("integration") or {}).get("hint", ""))
    lo = load_script()
    st = e.wait_for(lambda: (lambda s: s if s["window_control"] else None)(e.status()), 15.0) or e.status()
    if not st["window_control"]:
        print("loadScript:", lo.strip())
        print("daemon:", "\n".join((e.base / "daemon.out").read_text().splitlines()[-15:]))
    caps = e.cli_json("diagnose")["capabilities"]
    check("K01", "KWin script attaches over D-Bus; window control available on KWin Wayland", st["window_control"] and st["adapter"] == "kwin", str(caps.get("compositor_version")))

    # Impostor: any session process can call the bus name; it must be refused.
    out = subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=io.fractile.Bridge", "/io/fractile/Bridge", "io.fractile.Bridge1.Attach",
                          'string:{"kind":"kwin","api":1,"operations":{}}'], capture_output=True, text=True)
    out2 = subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=io.fractile.Bridge", "/io/fractile/Bridge", "io.fractile.Bridge1.Event",
                           'string:{"type":"hotkey","binding_id":"panel","timestamp":0}'], capture_output=True, text=True)
    out3 = subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=io.fractile.Bridge", "/io/fractile/Bridge", "io.fractile.Bridge1.Poll", "int32:10"],
                          capture_output=True, text=True)
    denied = all("AccessDenied" in o.stderr for o in (out, out2, out3))
    check("SEC6", "a non-KWin process cannot attach, poll for calls or inject events over D-Bus", denied, out.stderr.strip()[:140])

    e.window("gtk4", "org.fractile.TestGtk", "Gtk window")
    e.window("qt", "org.fractile.TestQt", "Qt window")
    gtk = e.wait_for(lambda: next((w for w in e.windows() if w.get("title") == "Gtk window"), None), 15.0, 0.3)
    qt = e.wait_for(lambda: next((w for w in e.windows() if w.get("title") == "Qt window"), None), 15.0, 0.3)
    check("K02", "native Wayland GTK4 and Qt windows are listed with app id and pid", bool(gtk and qt) and gtk.get("pid") and qt.get("app_id"),
          f"{gtk and gtk.get('app_id')} / {qt and qt.get('app_id')}")
    if not (gtk and qt):
        return
    g, q = gtk["id"], qt["id"]
    outs = e.cli_json("list-outputs")
    o = outs[0]
    check("K03", "outputs are listed with a stable id and a work area", len(outs) >= 1 and o["stable_id"] and o["work_area"]["w"] > 0, f"{o['stable_id']} {o['work_area']}")
    wa = o["work_area"]

    ok_all, detail = True, []
    for grid, cells in [("6x6", "0,0,2,5"), ("4x4", "1,1,2,3"), ("3x2", "2,0,2,1"), ("10x10", "0,0,9,9")]:
        out = e.cli_json("place", "--grid", grid, "--cells", cells, "--window", g)
        ok = out["within_tolerance"] and rect_eq(out["requested"], e.frame(g), 2)
        ok_all &= ok
        detail.append(f"{grid}:{cells} err={out['max_error_px']}px")
    check("K04", "grid placements land on the requested rectangle (GTK4)", ok_all, "; ".join(detail))
    out = e.cli_json("place", "--grid", "6x6", "--cells", "3,0,5,2", "--window", q)
    check("K05", "grid placement on a Qt Wayland window", out["within_tolerance"] and rect_eq(out["requested"], e.frame(q), 2), f"err={out['max_error_px']}px")
    e.cli_json("place", "--grid", "1x1", "--cells", "0,0,0,0", "--window", g)
    check("K06", "1x1 selection fills the work area exactly", rect_eq(e.frame(g), wa, 2), f"{e.frame(g)} vs {wa}")
    before = e.frame(g)
    e.cli_json("snap", "right-half", "--window", g)
    e.cli_json("undo")
    check("K07", "undo restores the previous geometry", rect_eq(e.frame(g), before, 2))

    # activation
    e.call("activate_window", {"window": q})
    check("K08", "activating a window focuses it", e.wait_for(lambda: e.call("active_window") == q, 3.0))

    # global shortcut through KGlobalAccel
    cfg = e.call("get_config")["config"]
    cfg["general"]["first_run_complete"] = True
    cfg["general"]["panel_shortcut"] = "Ctrl+Alt+G"
    cfg["shortcuts"].append({"id": "t-left-third", "name": "Left third", "scope": "global", "accelerator": "Ctrl+Alt+1", "cycle_monitors": True,
                             "action": {"type": "place", "grid": {"cols": 3, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}})
    r = e.call("set_config", {"config": cfg})
    keys = dbus("--dest=org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel.shortcut", "array:string:fractile,t-left-third,Fractile,t-left-third", check=False)
    registered = str(0x0C000031) in keys
    e.call("activate_window", {"window": q})
    e.wait_for(lambda: e.call("active_window") == q, 3.0)
    invoke_shortcut("t-left-third")
    target = {"x": wa["x"], "y": wa["y"], "w": wa["w"] // 3, "h": wa["h"]}
    placed = e.wait_for(lambda: rect_eq(e.frame(q), target, 3), 3.0, 0.05)
    check("K09", "shortcut registered with KDE (KGlobalAccel) and its activation places the focused window",
          registered and placed and not r["hotkey_errors"], f"registered={registered} errors={r['hotkey_errors']} frame={e.frame(q)}")

    # workspaces
    wss = e.call("list_workspaces")
    if len(wss) < 2:
        dbus("--dest=org.kde.KWin", "/VirtualDesktopManager", "org.kde.KWin.VirtualDesktopManager.createDesktop", "uint32:1", "string:Two", check=False)
        time.sleep(0.5)
        wss = e.call("list_workspaces")
    moved = False
    if len(wss) >= 2:
        e.call("move_to_workspace", {"window": q, "workspace": 1})
        moved = e.wait_for(lambda: next((w.get("workspace") for w in e.windows(True) if w["id"] == q), None) == 1, 3.0)
        e.call("move_to_workspace", {"window": q, "workspace": 0})
    check("K10", "workspaces are listed and a window can be moved between them", moved, f"{len(wss)} workspaces")

    # snapshot restore
    e.cli_json("place", "--grid", "2x2", "--cells", "0,0,0,1", "--window", g)
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", q)
    want_g, want_q = e.frame(g), e.frame(q)
    e.cli_json("save", "--name", "kwin")
    e.cli_json("snap", "center", "--window", g)
    e.cli_json("snap", "top-left-quarter", "--window", q)
    rep = e.cli_json("restore", "--snapshot", "kwin")
    check("K11", "snapshot restore puts KWin windows back exactly", rep["state"] == "completed" and rect_eq(e.frame(g), want_g, 2) and rect_eq(e.frame(q), want_q, 2))

    # margins and gaps (DV-22, DV-23), checked with independent arithmetic as in X25
    e.call("set_config", {"patch": {"appearance": {"spacing": {"margins_enabled": True, "gaps_enabled": True}}}})
    e.cli_json("snap", "left-half", "--window", g)
    e.cli_json("snap", "right-half", "--window", q)
    time.sleep(0.5)
    fg, fq = e.frame(g), e.frame(q)
    m = gap = 10
    t = 2  # what the compositor rounds (the other placement checks use the same)
    ok = bool(fg and fq) and (abs(fg["x"] - (wa["x"] + m)) <= t and abs(fg["y"] - (wa["y"] + m)) <= t and abs(fg["h"] - (wa["h"] - 2 * m)) <= t
          and abs(fq["y"] - (wa["y"] + m)) <= t and abs(fq["x"] + fq["w"] - (wa["x"] + wa["w"] - m)) <= t
          and abs(fq["x"] - (fg["x"] + fg["w"]) - gap) <= t and abs(fg["w"] - fq["w"]) <= t)
    check("K20", "10px margins and a 10px gap: halves sit inside the work area with the gap between them", ok, f"work area {wa}; left {fg}; right {fq}")
    e.call("set_config", {"patch": {"appearance": {"spacing": {"margins_enabled": False, "gaps_enabled": False}}}})

    # UI panel via the panel shortcut
    e.call("activate_window", {"window": g})
    invoke_shortcut("panel")
    panel = e.wait_for(lambda: next((w for w in e.windows(True) if w["title"].startswith("Fractile panel")), None), 12.0, 0.3)
    focused = panel and e.wait_for(lambda: e.call("active_window") == panel["id"], 3.0)
    inside = panel and panel["frame"]["x"] >= wa["x"] and panel["frame"]["y"] >= wa["y"] and panel["frame"]["x"] + panel["frame"]["w"] <= wa["x"] + wa["w"]
    check("K12", "panel shortcut starts the Wayland UI; panel shown, focused, inside the work area and flagged own",
          bool(panel) and focused and inside and panel["is_own"], f"{panel and panel['frame']}")
    listed = {w["id"] for w in e.windows()}
    own = [w["id"] for w in e.windows(True) if w["is_own"]]
    check("K13", "Fractile UI windows are never offered as placement targets", own and not (set(own) & listed), f"{len(own)} own")
    if panel:
        e.call("hide_panel")

    # script unloaded: honest status; reloaded: back
    unload_script()
    off = e.wait_for(lambda: not e.status()["window_control"], 10.0)
    p = e.run("place", "--grid", "2x2", "--cells", "0,0,0,0", "--window", g)
    load_script()
    back = e.wait_for(lambda: e.status()["window_control"], 15.0)
    check("K14", "script unloaded: no window control and placement refused; reloaded: control returns", off and p.returncode != 0 and back, f"place exit {p.returncode}")

    e.stop_daemon()
    e.start_daemon()
    back = e.wait_for(lambda: e.status()["window_control"], 15.0)
    check("K15", "daemon restart: the script re-attaches without user action", back)
    tb = [ln for ln in (e.ui_log.read_text().splitlines() if e.ui_log.exists() else []) if "Traceback" in ln]
    check("K16", "UI logged no Python errors", not tb, "; ".join(tb[:2]))

    # orderly exit releases the KDE shortcuts (no ghost bindings)
    before = dbus("--dest=org.kde.kglobalaccel", "/component/fractile", "org.kde.kglobalaccel.Component.shortcutNames", check=False)
    e.stop_daemon()
    after = dbus("--dest=org.kde.kglobalaccel", "/component/fractile", "org.kde.kglobalaccel.Component.shortcutNames", check=False)
    check("K17", "on exit Fractile unregisters its KDE global shortcuts",
          '"panel"' in before and '"panel"' not in after and '"t-left-third"' not in after, f"after exit: {' '.join(after.split())[:120]}")


def main():
    e = Env()
    try:
        run_all(e)
    except Exception as ex:
        check("KXX", "suite crashed", False, repr(ex))
    finally:
        for pr in e.procs:
            pr.terminate()
        e.stop_daemon()
    kv = subprocess.run(["kwin_wayland", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "e2e-kwin-wayland", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"{kv} --virtual (Wayland, no Xwayland) in an archlinux container (render device: {os.environ.get('FRACTILE_TEST_GPU', 'none')}); Fractile KWin script", "results": results}
    out = Path(os.environ.get("EVIDENCE", "/tmp/e2e-kwin.json"))
    out.write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    if failed and (e.base / "daemon.out").exists():
        print("== daemon log (warnings)")
        print("\n".join([ln for ln in (e.base / "daemon.out").read_text().splitlines() if " Warn " in ln or " Error " in ln][-25:]))
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
