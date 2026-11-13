#!/usr/bin/env python3
"""End-to-end tests on Sway through Fractile's Sway adapter.

Runs *inside* the test container (tests/sway/e2e-sway-in-container.sh):
kwin_wayland --virtual is only the parent display; Sway runs nested in it
(wlroots' Wayland backend) with its own Wayland socket. The suite starts
Sway with a user configuration of its own, an isolated fractiled, native
Wayland GTK4/Qt test windows and the real Fractile UI.

Shortcuts are pressed with wtype through Sway's virtual-keyboard protocol,
so Sway decodes the keys and runs its binding like a physical key press.
This is test-harness input only; Fractile itself never injects input.
"""
from __future__ import annotations

import json
import os
import signal
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
RT = Path(os.environ["XDG_RUNTIME_DIR"])
PWNED = Path("/tmp/fractile-sway-pwned")
results: list[dict] = []


def check(tid, name, ok, detail=""):
    ok = bool(ok)
    results.append({"id": tid, "name": name, "ok": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {tid} {name}" + (f" :: {detail}" if detail else ""), flush=True)
    return ok


def rect_eq(a, b, tol=0):
    return bool(a and b and all(abs(a[k] - b[k]) <= tol for k in ("x", "y", "w", "h")))


def inside(r, o):
    return r["x"] >= o["x"] and r["y"] >= o["y"] and r["x"] + r["w"] <= o["x"] + o["w"] and r["y"] + r["h"] <= o["y"] + o["h"]


class Sway:
    """The nested Sway compositor."""

    def __init__(self):
        self.proc = None
        self.sock = None
        self.display = None

    def start(self):
        before = {p.name for p in RT.iterdir()}
        env = dict(os.environ, WAYLAND_DISPLAY="wl-0", WLR_BACKENDS="wayland", WLR_RENDERER="pixman")
        env.pop("SWAYSOCK", None)
        self.proc = subprocess.Popen(["sway", "-d"], env=env, stdout=open(os.environ.get("SWAY_LOG", "/tmp/sway.log"), "a"), stderr=subprocess.STDOUT)
        end = time.time() + 30
        while time.time() < end and not (self.sock and self.display):
            new = {p.name for p in RT.iterdir()} - before
            socks = [n for n in new if n.startswith("sway-ipc.") and n.endswith(f".{self.proc.pid}.sock")]
            disp = [n for n in new if n.startswith("wayland-") and not n.endswith(".lock")]
            if socks and disp:
                self.sock, self.display = str(RT / socks[0]), disp[0]
            else:
                time.sleep(0.2)
        if not self.sock:
            raise RuntimeError("Sway did not start")
        for _ in range(50):
            if self.msg("-t", "get_version").returncode == 0:
                return
            time.sleep(0.2)

    def env(self):
        return {"SWAYSOCK": self.sock, "WAYLAND_DISPLAY": self.display}

    def msg(self, *a):
        return subprocess.run(["swaymsg", *a], env=dict(os.environ, **self.env()), capture_output=True, text=True, timeout=20)

    def j(self, what):
        return json.loads(self.msg("-r", "-t", what).stdout)

    def cmd(self, c):
        p = self.msg("-r", c)
        try:
            return all(x.get("success") for x in json.loads(p.stdout))
        except ValueError:
            return False

    def node(self, wid):
        cid = int(wid.split(":", 1)[1])

        def walk(n):
            if n.get("id") == cid:
                return n
            for k in n.get("nodes", []) + n.get("floating_nodes", []):
                r = walk(k)
                if r:
                    return r
            return None
        return walk(self.j("get_tree"))

    def keys(self, *mods_and_key):
        *mods, key = mods_and_key
        a = ["wtype"]
        for m in mods:
            a += ["-M", m]
        a += ["-k", key]
        for m in reversed(mods):
            a += ["-m", m]
        return subprocess.run(a, env=dict(os.environ, WAYLAND_DISPLAY=self.display), capture_output=True, text=True, timeout=10)


class Env:
    def __init__(self, sway: Sway):
        self.s = sway
        self.base = Path(tempfile.mkdtemp(prefix="fractile-sway-"))
        self.env = dict(os.environ)
        self.env.update(
            XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="sway",
            FRACTILE_DATA_DIR=str(self.base / "data"), FRACTILE_CONFIG=str(self.base / "config.toml"),
            XDG_STATE_HOME=str(self.base / "state"), XDG_DATA_HOME=str(self.base / "xdgdata"),
            FRACTILE_NO_LOGIND="1", FRACTILE_AUTOSTART_MODE="none", FRACTILE_NO_SCOPE="1",
            QT_QPA_PLATFORM="wayland", GDK_BACKEND="wayland", GSETTINGS_BACKEND="memory", GSK_RENDERER="cairo",
        )
        self.env.update(sway.env())
        self.sock = RT / "fractile" / "fractile.sock"
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

    def call(self, m, p=None):
        c = Client("e2e-sway", self.sock)
        try:
            return c.call(m, p or {})
        finally:
            c.close()

    def start_daemon(self, env=None):
        self.daemon = subprocess.Popen([str(BIN / "fractiled"), "--verbose"], env=env or self.env, stdout=open(self.base / "daemon.out", "a"), stderr=subprocess.STDOUT)
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
        p = subprocess.Popen([sys.executable, str(TESTWIN), "--toolkit", tk, "--app-id", app, "--title", title], env=self.env,
                             stdout=subprocess.DEVNULL, stderr=open(self.base / f"win-{app}.log", "a"))
        self.procs.append(p)
        return p

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

    def win(self, title):
        return next((w for w in self.windows(True) if w.get("title") == title), None)

    def frame(self, wid):
        return next((w["frame"] for w in self.windows(True) if w["id"] == wid), None)

    def status(self):
        return self.cli_json("status")

    def log_tail(self, n=15):
        return "\n".join((self.base / "daemon.out").read_text().splitlines()[-n:])


def bound(s: Sway, combo: str) -> bool:
    """Whether a runtime binding exists on these keys, found by trying to
    remove it (`unbindsym` fails for keys with no binding). Only used where
    the binding is meant to be gone or is about to be removed anyway."""
    return s.cmd(f"unbindsym --no-repeat {combo}")


def run_all(s: Sway, e: Env):
    e.start_daemon()
    st = e.status()
    caps = e.cli_json("diagnose")["capabilities"]
    ok = st["window_control"] and st["adapter"] == "sway"
    check("S00", "Sway is detected and the adapter connects over Sway's IPC; window control available", ok, f"Sway {caps.get('compositor_version')}")
    if not ok:
        print(e.log_tail())
        return
    bad = dict(e.env, SWAYSOCK=str(RT / "sway-ipc.0.0.sock"))
    out = subprocess.run([str(BIN / "fractiled"), "--check"], env=bad, capture_output=True, text=True, timeout=30)
    bc = json.loads(out.stdout)["capabilities"]
    why = bc["operations"]["move_resize"].get("note", "")
    check("S23", "Sway detected but its socket unreachable: every operation unsupported, with the reason",
          bc["adapter"] == "sway" and {v["level"] for v in bc["operations"].values()} == {"unsupported"} and "sway-ipc.0.0.sock" in why, why[:160])
    levels = {k: v["level"] for k, v in caps["operations"].items()}
    check("S01", "capabilities are reported honestly (placement partial: tiled windows float; no preview overlay)",
          levels["move_resize"] == "partial" and levels["show_overlay"] == "unsupported" and levels["list_windows"] == "full", json.dumps(levels)[:160])

    e.window("gtk4", "org.fractile.TestGtk", "Gtk window")
    e.window("qt", "org.fractile.TestQt", "Qt window")
    gtk = e.wait_for(lambda: next((w for w in e.windows() if w.get("title") == "Gtk window"), None), 15.0, 0.3)
    qt = e.wait_for(lambda: next((w for w in e.windows() if w.get("title") == "Qt window"), None), 15.0, 0.3)
    check("S02", "native Wayland GTK4 and Qt windows are listed with app id, pid and workspace; Sway tiled them",
          bool(gtk and qt) and gtk.get("pid") and qt.get("app_id") == "org.fractile.TestQt" and gtk.get("workspace") == 0 and gtk["state"]["tiled"],
          f"{gtk and gtk.get('app_id')} / {qt and qt.get('app_id')} ws={gtk and gtk.get('workspace')}")
    if not (gtk and qt):
        return
    g, q = gtk["id"], qt["id"]
    outs = e.cli_json("list-outputs")
    o = outs[0]
    check("S03", "outputs are listed with logical size, a stable id and a work area", len(outs) == 1 and o["rect"]["w"] == 1600 and o["stable_id"] and o["work_area"]["w"] > 0,
          f"{o['stable_id']} {o['rect']} {o['work_area']}")
    wa = o["work_area"]

    ok_all, detail = True, []
    for grid, cells in [("6x6", "0,0,2,5"), ("4x4", "1,1,2,3"), ("3x2", "2,0,2,1"), ("10x10", "0,0,9,9")]:
        out = e.cli_json("place", "--grid", grid, "--cells", cells, "--window", g)
        ok = out["within_tolerance"] and rect_eq(out["requested"], e.frame(g), 2)
        ok_all &= ok
        detail.append(f"{grid}:{cells} err={out['max_error_px']}px" + ("" if ok else f" listed {e.frame(g)} vs {out['requested']}"))
    n = s.node(g)
    check("S04", "grid placements land on the requested rectangle (GTK4); the placed window is now floating", ok_all and n and n["type"] == "floating_con",
          "; ".join(detail) + f"; sway node type {n and n.get('type')} (found {n is not None})")
    out = e.cli_json("place", "--grid", "6x6", "--cells", "3,0,5,2", "--window", q)
    check("S05", "grid placement on a Qt Wayland window", out["within_tolerance"] and rect_eq(out["requested"], e.frame(q), 2), f"err={out['max_error_px']}px note={out.get('note')}")
    e.cli_json("place", "--grid", "1x1", "--cells", "0,0,0,0", "--window", g)
    n = s.node(g)
    check("S06", "1x1 selection fills the work area exactly (border included in the frame)", rect_eq(e.frame(g), wa, 2),
          f"{e.frame(g)} vs {wa}; sway rect {n and n['rect']} window_rect {n and n['window_rect']}")
    before = e.frame(g)
    e.cli_json("snap", "right-half", "--window", g)
    e.cli_json("undo")
    check("S07", "undo restores the previous geometry", rect_eq(e.frame(g), before, 2))

    e.call("activate_window", {"window": q})
    check("S08", "activating a window focuses it", e.wait_for(lambda: e.call("active_window") == q, 3.0))

    s.cmd(f"[con_id={q.split(':')[1]}] fullscreen enable")
    fs = e.wait_for(lambda: (s.node(q) or {}).get("fullscreen_mode") == 1, 3.0)
    out = e.cli_json("place", "--grid", "2x2", "--cells", "1,0,1,0", "--window", q)
    check("S09", "a fullscreen window leaves fullscreen and lands on the cell", fs and out["within_tolerance"] and (s.node(q) or {}).get("fullscreen_mode") == 0,
          f"was fullscreen {fs}, err={out['max_error_px']}px")

    # Global shortcuts as runtime bindsym, pressed through the keyboard path.
    cfg = e.call("get_config")["config"]
    cfg["general"]["first_run_complete"] = True
    cfg["general"]["panel_shortcut"] = "Ctrl+Alt+G"
    cfg["shortcuts"].append({"id": "t-left-third", "name": "Left third", "scope": "global", "accelerator": "Ctrl+Alt+1", "cycle_monitors": True,
                             "action": {"type": "place", "grid": {"cols": 3, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}})
    r = e.call("set_config", {"config": cfg})
    e.call("activate_window", {"window": q})
    e.wait_for(lambda: e.call("active_window") == q, 3.0)
    k = s.keys("ctrl", "alt", "1")
    target = {"x": wa["x"], "y": wa["y"], "w": wa["w"] // 3, "h": wa["h"]}
    placed = e.wait_for(lambda: rect_eq(e.frame(q), target, 3), 4.0, 0.05)
    check("S10", "shortcut registered as a Sway binding; pressing the keys places the focused window",
          placed and not r["hotkey_errors"], f"errors={r['hotkey_errors']} wtype={k.returncode}{k.stderr.strip()[:80]} frame={e.frame(q)}")

    # Keys the user's configuration binds (main file, and a file it
    # includes) are conflicts, and the user's bindings keep working.
    cfg["shortcuts"].append({"id": "t-conflict", "name": "Conflict", "scope": "global", "accelerator": "Super+Q",
                             "action": {"type": "place", "grid": {"cols": 2, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}})
    cfg["shortcuts"].append({"id": "t-conflict-inc", "name": "Conflict include", "scope": "global", "accelerator": "Super+Shift+T",
                             "action": {"type": "place", "grid": {"cols": 2, "rows": 1}, "selection": {"col0": 1, "row0": 0, "col1": 1, "row1": 0}}})
    r = e.call("set_config", {"config": cfg})
    errs = json.dumps(r["hotkey_errors"])
    victim = e.window("gtk4", "org.fractile.Victim", "Victim")
    vw = e.wait_for(lambda: e.win("Victim"), 10.0, 0.2)
    if vw:
        e.call("activate_window", {"window": vw["id"]})
        e.wait_for(lambda: e.call("active_window") == vw["id"], 3.0)
    s.keys("logo", "q")  # the user's own binding: kill
    killed = e.wait_for(lambda: victim.poll() is not None or not e.win("Victim"), 5.0, 0.2)
    marker = Path("/tmp/fractile-sway-user-bind")
    s.keys("logo", "shift", "t")  # the included file's binding: exec touch
    touched = e.wait_for(lambda: marker.exists(), 5.0, 0.2)
    check("S11", "keys the Sway configuration binds (main file and an included file) are reported as conflicts; the user's bindings still run",
          "t-conflict" in errs and "t-conflict-inc" in errs and "kill" in errs and bool(killed) and bool(touched), f"{errs[:300]}; user kill ran {bool(killed)}; included exec ran {bool(touched)}")
    cfg["shortcuts"] = [x for x in cfg["shortcuts"] if not x["id"].startswith("t-conflict")]

    # Hostile data: a shortcut id and a window title with Sway command
    # syntax in them never reach a command.
    evil = 'x; exec touch /tmp/fractile-sway-pwned; nop'
    cfg["shortcuts"].append({"id": evil, "name": "Evil", "scope": "global", "accelerator": "Ctrl+Alt+8",
                             "action": {"type": "place", "grid": {"cols": 2, "rows": 1}, "selection": {"col0": 1, "row0": 0, "col1": 1, "row1": 0}}})
    try:
        e.call("set_config", {"config": cfg})
        err = ""
    except Exception as ex:  # a rejected id is fine too
        err = str(ex)
    title = '"] exec touch /tmp/fractile-sway-pwned ['
    e.window("gtk4", "org.fractile.Evil", title)
    ew = e.wait_for(lambda: e.win(title), 10.0, 0.2)
    if ew:
        e.cli_json("place", "--grid", "2x2", "--cells", "0,1,0,1", "--window", ew["id"])
    s.keys("ctrl", "alt", "8")
    time.sleep(0.8)
    check("SEC7", "a shortcut id and a window title with Sway command syntax stay data (nothing runs in Sway)",
          not PWNED.exists() and bool(ew), f"id {'rejected: ' + err[:60] if err else 'bound'}; hostile-title window placed {bool(ew)}")
    cfg["shortcuts"] = [x for x in cfg["shortcuts"] if x["id"] != evil]
    e.call("set_config", {"config": cfg})

    e.call("move_to_workspace", {"window": q, "workspace": 1})
    moved = e.wait_for(lambda: next((w.get("workspace") for w in e.windows(True) if w["id"] == q), None) == 1, 3.0)
    wss = e.call("list_workspaces")
    e.call("move_to_workspace", {"window": q, "workspace": 0})
    back = e.wait_for(lambda: next((w.get("workspace") for w in e.windows(True) if w["id"] == q), None) == 0, 3.0)
    check("S12", "a window moves to another workspace (created on demand) and back", moved and back, f"workspaces seen: {[w['name'] for w in wss]}")

    e.cli_json("place", "--grid", "2x2", "--cells", "0,0,0,1", "--window", g)
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", q)
    want_g, want_q = e.frame(g), e.frame(q)
    e.cli_json("save", "--name", "sway")
    e.cli_json("snap", "center", "--window", g)
    e.cli_json("snap", "top-left-quarter", "--window", q)
    rep = e.cli_json("restore", "--snapshot", "sway")
    check("S13", "snapshot restore puts Sway windows back exactly", rep["state"] == "completed" and rect_eq(e.frame(g), want_g, 2) and rect_eq(e.frame(q), want_q, 2))

    e.call("activate_window", {"window": g})
    s.keys("ctrl", "alt", "g")
    panel = e.wait_for(lambda: next((w for w in e.windows(True) if w["title"].startswith("Fractile panel")), None), 15.0, 0.3)
    focused = panel and e.wait_for(lambda: e.call("active_window") == panel["id"], 3.0)
    pn = panel and s.node(panel["id"])
    ok = bool(panel) and focused and inside(panel["frame"], wa) and panel["is_own"] and pn and pn["type"] == "floating_con"
    check("S14", "panel shortcut starts the Wayland UI; the panel floats (Fractile's own for_window rule), is focused, inside the work area and flagged own",
          ok, f"{panel and panel['frame']} type={pn and pn['type']}")
    listed = {w["id"] for w in e.windows()}
    own = [w["id"] for w in e.windows(True) if w["is_own"]]
    check("S15", "Fractile UI windows are never offered as placement targets", own and not (set(own) & listed), f"{len(own)} own")
    if panel:
        time.sleep(0.5)
        subprocess.run(["wtype", "-k", "Right", "-k", "Right", "-k", "Right", "-M", "shift", "-k", "Right", "-k", "Right", "-k", "Down", "-k", "Down",
                        "-m", "shift", "-k", "Return"], env=dict(os.environ, WAYLAND_DISPLAY=s.display), timeout=10)
        want = e.call("compute_rect", {"grid": {"cols": 6, "rows": 6}, "selection": {"col0": 2, "row0": 0, "col1": 4, "row1": 2}})
        got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want, 2) else None)(e.frame(g)), 4.0) or e.frame(g)
        gone = e.wait_for(lambda: not any(w["title"].startswith("Fractile panel") for w in e.windows(True)), 3.0)
        back = e.wait_for(lambda: e.call("active_window") == g, 3.0)
        check("S22", "panel keyboard selection (arrows, Shift+arrows, Enter) places the window; the panel closes and focus returns",
              rect_eq(got, want, 2) and gone and back, f"want {want} got {got}; closed {bool(gone)} focus back {bool(back)}")
        if not gone:
            e.call("hide_panel")

    # `swaymsg reload` drops runtime bindings (a marker binding shows it);
    # Fractile's come back and work.
    s.cmd("bindsym --no-repeat Ctrl+Mod1+9 nop reload-marker")
    s.msg("reload")
    time.sleep(1.5)
    dropped = not bound(s, "Ctrl+Mod1+9")
    e.call("activate_window", {"window": q})
    e.wait_for(lambda: e.call("active_window") == q, 3.0)
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", q)
    s.keys("ctrl", "alt", "1")
    placed = e.wait_for(lambda: rect_eq(e.frame(q), target, 3), 4.0, 0.05)
    check("S16", "after `swaymsg reload` the shortcuts are bound again and still work", dropped and placed, f"marker binding dropped by the reload: {dropped}; shortcut placed {bool(placed)}")

    # A second output (created on the Wayland backend) and moving a window there.
    names0 = {x["name"] for x in s.j("get_outputs")}
    s.cmd("create_output")
    new = e.wait_for(lambda: [x["name"] for x in s.j("get_outputs") if x["name"] not in names0], 5.0, 0.2) or []
    if new:
        s.cmd(f"output {new[0]} mode 1280x1024 position 1600 0")
    two = e.wait_for(lambda: (lambda os_: len(os_) == 2 and any(x["rect"]["x"] == 1600 for x in os_))(e.cli_json("list-outputs")), 5.0, 0.2)
    moved = False
    detail = f"created {new}"
    if two:
        outs = e.cli_json("list-outputs")
        other = next(x for x in outs if x["stable_id"] != o["stable_id"])
        e.cli_json("move-output", "next", "--window", g)
        moved = e.wait_for(lambda: (lambda w: w and w.get("output_id") == other["stable_id"] and inside(w["frame"], other["work_area"]))(
            next((w for w in e.windows(True) if w["id"] == g), None)), 4.0, 0.1)
        detail = f"second output {other['name']} {other['rect']}; window {e.frame(g)} output {next((w.get('output_id') for w in e.windows(True) if w['id'] == g), None)}"
        e.cli_json("move-output", "next", "--window", g)
    check("S17", "a second output is listed, and a window moves onto it", two and moved, detail)
    if new:
        s.cmd(f"output {new[0]} unplug")

    # Orderly exit removes the bindings; a crash leaves them, and the next
    # service (found without SWAYSOCK) binds over them.
    e.stop_daemon()
    left = [c for c in ("Ctrl+Mod1+1", "Ctrl+Mod1+g") if bound(s, c)]
    check("S18", "on exit Fractile removes its Sway bindings", not left, f"left: {left}")
    e.start_daemon()
    time.sleep(1.0)
    os.kill(e.daemon.pid, signal.SIGKILL)
    e.daemon.wait(5)
    env2 = dict(e.env)
    env2.pop("SWAYSOCK")
    e.start_daemon(env2)
    ctl = e.wait_for(lambda: e.status()["window_control"], 10.0)
    e.call("activate_window", {"window": q})
    e.wait_for(lambda: e.call("active_window") == q, 3.0)
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", q)
    time.sleep(1.0)  # the service answers IPC before its event subscription is up
    s.keys("ctrl", "alt", "1")
    placed = e.wait_for(lambda: rect_eq(e.frame(q), target, 3), 4.0, 0.05)
    check("S19", "after a crash, a new service (without SWAYSOCK) finds Sway, binds over the stale bindings, and the shortcut works",
          ctl and placed, f"control={ctl} placed={placed} frame={e.frame(q)}")

    tb = [ln for ln in (e.ui_log.read_text().splitlines() if e.ui_log.exists() else []) if "Traceback" in ln]
    check("S20", "UI logged no Python errors", not tb, "; ".join(tb[:2]))

    s.msg("exit")
    s.proc.wait(15)
    gone = e.wait_for(lambda: e.daemon.poll() is not None, 15.0, 0.2)
    log = (e.base / "daemon.out").read_text()
    check("S21", "when Sway exits the service saves the last seen layout and exits", gone and "display closed" in log, e.log_tail(3).replace("\n", " | ")[-200:])


def main():
    s = Sway()
    e = None
    PWNED.unlink(missing_ok=True)
    try:
        s.start()
        e = Env(s)
        run_all(s, e)
    except Exception as ex:
        import traceback
        traceback.print_exc()
        check("SXX", "suite crashed", False, repr(ex))
    finally:
        if e:
            for pr in e.procs:
                pr.terminate()
            e.stop_daemon()
        if s.proc and s.proc.poll() is None:
            s.proc.terminate()
    sv = subprocess.run(["sway", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "e2e-sway", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"{sv or 'sway'} nested in kwin_wayland --virtual (wlroots Wayland backend, pixman renderer), archlinux container; keys pressed with wtype",
          "results": results}
    out = Path(os.environ.get("EVIDENCE", "/tmp/e2e-sway.json"))
    out.write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    if failed and e and (e.base / "daemon.out").exists():
        print("== daemon log (warnings)")
        print("\n".join([ln for ln in (e.base / "daemon.out").read_text().splitlines() if " Warn " in ln or " Error " in ln][-25:]))
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
