#!/usr/bin/env python3
"""End-to-end tests on Hyprland through Fractile's Hyprland adapter.

Runs *inside* the test container (tests/hyprland/e2e-hyprland-in-container.sh):
kwin_wayland --virtual is only the parent display; Hyprland runs nested in it
with its own Wayland socket. The suite starts Hyprland, an isolated
fractiled, native Wayland GTK4/Qt test windows and the real Fractile UI.

Shortcuts are pressed with wtype through Hyprland's virtual-keyboard
protocol, so Hyprland decodes the keys and runs its bind like a physical key
press. This is test-harness input only; Fractile itself never injects input.
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


class Hypr:
    """The nested Hyprland compositor."""

    def __init__(self):
        self.proc = None
        self.sig = None
        self.display = None

    def start(self):
        env = dict(os.environ, WAYLAND_DISPLAY="wl-0", HYPRLAND_NO_CRASHREPORTER="1")
        env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        self.proc = subprocess.Popen(["Hyprland"], env=env, stdout=open(os.environ.get("HYPRLAND_LOG", "/tmp/hyprland.log"), "a"), stderr=subprocess.STDOUT)
        end = time.time() + 30
        while time.time() < end and not self.sig:
            # `hyprctl instances` names each instance and its Wayland socket.
            p = subprocess.run(["hyprctl", "-j", "instances"], capture_output=True, text=True, timeout=10)
            try:
                inst = [i for i in json.loads(p.stdout) if i.get("pid") == self.proc.pid]
            except ValueError:
                inst = []
            if inst and inst[0].get("wl_socket"):
                self.sig, self.display = inst[0]["instance"], inst[0]["wl_socket"]
            else:
                time.sleep(0.3)
        if not self.sig:
            raise RuntimeError("Hyprland did not start")
        for _ in range(50):
            if self.ctl("version").returncode == 0:
                return
            time.sleep(0.2)

    def env(self):
        return {"HYPRLAND_INSTANCE_SIGNATURE": self.sig, "WAYLAND_DISPLAY": self.display}

    def ctl(self, *a):
        return subprocess.run(["hyprctl", *a], env=dict(os.environ, **self.env()), capture_output=True, text=True, timeout=20)

    def j(self, what):
        return json.loads(self.ctl("-j", *what.split(" ")).stdout)

    def dispatch(self, lua):
        return self.ctl("dispatch", lua).stdout.strip()

    def ours(self):
        return [b for b in self.j("binds") if b.get("description", "").startswith("Fractile: ")]

    def client(self, wid):
        addr = wid.split(":", 1)[1]
        return next((c for c in self.j("clients") if c["address"].lower() == addr), None)

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
    def __init__(self, hypr: Hypr):
        self.h = hypr
        self.base = Path(tempfile.mkdtemp(prefix="fractile-hypr-"))
        self.env = dict(os.environ)
        self.env.update(
            XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="Hyprland",
            FRACTILE_DATA_DIR=str(self.base / "data"), FRACTILE_CONFIG=str(self.base / "config.toml"),
            XDG_STATE_HOME=str(self.base / "state"), XDG_DATA_HOME=str(self.base / "xdgdata"),
            FRACTILE_NO_LOGIND="1", FRACTILE_AUTOSTART_MODE="none", FRACTILE_NO_SCOPE="1",
            QT_QPA_PLATFORM="wayland", GDK_BACKEND="wayland", GSETTINGS_BACKEND="memory",
        )
        self.env.update(hypr.env())
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
        c = Client("e2e-hyprland", self.sock)
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

    def window(self, tk, app, title, *extra):
        self.procs.append(subprocess.Popen([sys.executable, str(TESTWIN), "--toolkit", tk, "--app-id", app, "--title", title, *extra], env=self.env,
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

    def log_tail(self, n=15):
        return "\n".join((self.base / "daemon.out").read_text().splitlines()[-n:])


def run_all(h: Hypr, e: Env):
    e.start_daemon()
    st = e.status()
    caps = e.cli_json("diagnose")["capabilities"]
    ok = st["window_control"] and st["adapter"] == "hyprland"
    check("H00", "Hyprland is detected and the adapter connects over Hyprland's IPC; window control available", ok, f"Hyprland {caps.get('compositor_version')}")
    if not ok:
        print(e.log_tail())
        return
    # A Hyprland session whose instance cannot be reached: reported, with the reason.
    bad = dict(e.env, HYPRLAND_INSTANCE_SIGNATURE="0000_not_running")
    out = subprocess.run([str(BIN / "fractiled"), "--check"], env=bad, capture_output=True, text=True, timeout=30)
    bc = json.loads(out.stdout)["capabilities"]
    why = bc["operations"]["move_resize"].get("note", "")
    check("H23", "Hyprland detected but its instance unreachable: every operation unsupported, with the reason",
          bc["adapter"] == "hyprland" and {v["level"] for v in bc["operations"].values()} == {"unsupported"} and "0000_not_running" in why, why[:160])
    levels = {k: v["level"] for k, v in caps["operations"].items()}
    check("H01", "capabilities are reported honestly (placement partial: tiled windows float; no preview overlay)",
          levels["move_resize"] == "partial" and levels["show_overlay"] == "unsupported" and levels["list_windows"] == "full", json.dumps(levels)[:160])

    e.window("gtk4", "org.fractile.TestGtk", "Gtk window")
    e.window("qt", "org.fractile.TestQt", "Qt window")
    gtk = e.wait_for(lambda: next((w for w in e.windows() if w.get("title") == "Gtk window"), None), 15.0, 0.3)
    qt = e.wait_for(lambda: next((w for w in e.windows() if w.get("title") == "Qt window"), None), 15.0, 0.3)
    check("H02", "native Wayland GTK4 and Qt windows are listed with app id, pid and workspace; Hyprland tiled them",
          bool(gtk and qt) and gtk.get("pid") and qt.get("app_id") == "org.fractile.TestQt" and gtk.get("workspace") == 0 and gtk["state"]["tiled"],
          f"{gtk and gtk.get('app_id')} / {qt and qt.get('app_id')} ws={gtk and gtk.get('workspace')}")
    if not (gtk and qt):
        return
    g, q = gtk["id"], qt["id"]
    outs = e.cli_json("list-outputs")
    o = outs[0]
    check("H03", "outputs are listed with logical size, a stable id and a work area", len(outs) == 1 and o["rect"]["w"] == 1600 and o["stable_id"] and o["work_area"]["w"] > 0,
          f"{o['stable_id']} {o['rect']} {o['work_area']}")
    wa = o["work_area"]

    ok_all, detail = True, []
    for grid, cells in [("6x6", "0,0,2,5"), ("4x4", "1,1,2,3"), ("3x2", "2,0,2,1"), ("10x10", "0,0,9,9")]:
        out = e.cli_json("place", "--grid", grid, "--cells", cells, "--window", g)
        ok = out["within_tolerance"] and rect_eq(out["requested"], e.frame(g), 2)
        ok_all &= ok
        detail.append(f"{grid}:{cells} err={out['max_error_px']}px")
    c = h.client(g)
    check("H04", "grid placements land on the requested rectangle (GTK4); the placed window is now floating", ok_all and c and c["floating"], "; ".join(detail))
    out = e.cli_json("place", "--grid", "6x6", "--cells", "3,0,5,2", "--window", q)
    check("H05", "grid placement on a Qt Wayland window", out["within_tolerance"] and rect_eq(out["requested"], e.frame(q), 2), f"err={out['max_error_px']}px note={out.get('note')}")
    e.cli_json("place", "--grid", "1x1", "--cells", "0,0,0,0", "--window", g)
    check("H06", "1x1 selection fills the work area exactly (border included in the frame)", rect_eq(e.frame(g), wa, 2), f"{e.frame(g)} vs {wa}; box {c and (c['at'], c['size'])}")
    before = e.frame(g)
    e.cli_json("snap", "right-half", "--window", g)
    e.cli_json("undo")
    check("H07", "undo restores the previous geometry", rect_eq(e.frame(g), before, 2))

    # A cell smaller than the window's minimum size (GNOME Calculator in a
    # real login): the client keeps its minimum, and the window keeps the
    # cell's top-left inside the work area instead of being pushed off the
    # screen by repeated corrections (Hyprland keeps the centre on a resize).
    size_file = os.path.join(tempfile.gettempdir(), "fractile-h24-size")
    e.window("gtk4", "org.fractile.TestMin", "Min window", "--min-size", "700x700", "--report-size", size_file)
    minp = e.procs[-1]
    mw = e.wait_for(lambda: next((w for w in e.windows() if w.get("title") == "Min window"), None), 15.0, 0.3)
    if mw:
        out = e.cli_json("place", "--grid", "4x4", "--cells", "0,2,0,2", "--window", mw["id"])
        time.sleep(1.0)
        try:
            cw, ch = (int(v) for v in open(size_file).read().split("x"))
        except (OSError, ValueError):
            cw = ch = 0
        fr, req = e.frame(mw["id"]) or {}, out["requested"]
        in_wa = bool(fr) and inside(fr, wa)
        want_y = min(req["y"], wa["y"] + wa["h"] - fr.get("h", 0))
        check("H24", "a cell smaller than the window's minimum size: the window keeps its minimum, the cell's left edge, and the cell's top "
                     "(moved up only as far as needed to stay inside the work area)",
              cw >= 700 and ch >= 700 and in_wa and abs(fr["x"] - req["x"]) <= 2 and abs(fr["y"] - want_y) <= 2,
              f"requested {req} frame {fr} client {cw}x{ch} work area {wa} attempts {out['attempts']} note {out.get('note')}")
        minp.terminate()
    else:
        check("H24", "a cell smaller than the window's minimum size: the window keeps its minimum inside the work area", False, "no window")

    e.call("activate_window", {"window": q})
    check("H08", "activating a window focuses it", e.wait_for(lambda: e.call("active_window") == q, 3.0))

    # Fullscreen window: placement leaves fullscreen first.
    h.dispatch(f'hl.dsp.window.fullscreen_state({{ internal = 2, client = 2, window = "address:{q.split(":")[1]}" }})')
    fs = e.wait_for(lambda: (h.client(q) or {}).get("fullscreen") == 2, 3.0)
    out = e.cli_json("place", "--grid", "2x2", "--cells", "1,0,1,0", "--window", q)
    check("H09", "a fullscreen window leaves fullscreen and lands on the cell", fs and out["within_tolerance"] and (h.client(q) or {}).get("fullscreen") == 0,
          f"was fullscreen {fs}, err={out['max_error_px']}px")

    # Global shortcuts as Hyprland binds, pressed through the keyboard path.
    cfg = e.call("get_config")["config"]
    cfg["general"]["first_run_complete"] = True
    cfg["general"]["panel_shortcut"] = "Ctrl+Alt+G"
    cfg["shortcuts"].append({"id": "t-left-third", "name": "Left third", "scope": "global", "accelerator": "Ctrl+Alt+1", "cycle_monitors": True,
                             "action": {"type": "place", "grid": {"cols": 3, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}})
    r = e.call("set_config", {"config": cfg})
    ours = h.ours()
    reg = {b["description"]: b["modmask"] for b in ours}
    registered = reg.get("Fractile: t-left-third") == 12 and reg.get("Fractile: panel") == 12
    e.call("activate_window", {"window": q})
    e.wait_for(lambda: e.call("active_window") == q, 3.0)
    k = h.keys("ctrl", "alt", "1")
    target = {"x": wa["x"], "y": wa["y"], "w": wa["w"] // 3, "h": wa["h"]}
    placed = e.wait_for(lambda: rect_eq(e.frame(q), target, 3), 4.0, 0.05)
    check("H10", "shortcut registered as a Hyprland bind; pressing the keys places the focused window",
          registered and placed and not r["hotkey_errors"], f"binds={reg} errors={r['hotkey_errors']} wtype={k.returncode}{k.stderr.strip()[:80]} frame={e.frame(q)}")

    # Keys the user's own Hyprland config binds are a conflict, and the
    # user's bind is untouched.
    cfg["shortcuts"].append({"id": "t-conflict", "name": "Conflict", "scope": "global", "accelerator": "Super+Q",
                             "action": {"type": "place", "grid": {"cols": 2, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}})
    r = e.call("set_config", {"config": cfg})
    user_bind = [b for b in h.j("binds") if b["modmask"] == 64 and b["key"].lower() == "q"]
    errs = json.dumps(r["hotkey_errors"])
    check("H11", "a key combination the Hyprland config already binds is reported as a conflict; the user's bind stays",
          "t-conflict" in errs and len(user_bind) == 1 and not user_bind[0].get("description", "").startswith("Fractile"), errs[:200])
    cfg["shortcuts"] = [s for s in cfg["shortcuts"] if s["id"] != "t-conflict"]

    # Hostile shortcut id: it goes into a Lua string literal and must stay data.
    evil = 'x") hl.exec_cmd("touch /tmp/fractile-pwned") --'
    cfg["shortcuts"].append({"id": evil, "name": "Evil", "scope": "global", "accelerator": "Ctrl+Alt+8",
                             "action": {"type": "place", "grid": {"cols": 2, "rows": 1}, "selection": {"col0": 1, "row0": 0, "col1": 1, "row1": 0}}})
    try:
        e.call("set_config", {"config": cfg})
        err = ""
    except Exception as ex:  # a rejected id is fine too
        err = str(ex)
    time.sleep(0.5)
    descs = [b["description"] for b in h.ours()]
    check("SEC7", "a shortcut id with Lua syntax in it stays data (no code runs in Hyprland)",
          not Path("/tmp/fractile-pwned").exists() and (f"Fractile: {evil}" in descs or err), f"rejected: {err[:80]}" if err else "bound with the literal id")
    cfg["shortcuts"] = [s for s in cfg["shortcuts"] if s["id"] != evil]
    e.call("set_config", {"config": cfg})

    # Workspaces
    e.call("move_to_workspace", {"window": q, "workspace": 1})
    moved = e.wait_for(lambda: next((w.get("workspace") for w in e.windows(True) if w["id"] == q), None) == 1, 3.0)
    wss = e.call("list_workspaces")
    e.call("move_to_workspace", {"window": q, "workspace": 0})
    back = e.wait_for(lambda: next((w.get("workspace") for w in e.windows(True) if w["id"] == q), None) == 0, 3.0)
    check("H12", "a window moves to another workspace (created on demand) and back", moved and back, f"workspaces seen: {[w['name'] for w in wss]}")

    # Snapshot restore
    e.cli_json("place", "--grid", "2x2", "--cells", "0,0,0,1", "--window", g)
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", q)
    want_g, want_q = e.frame(g), e.frame(q)
    e.cli_json("save", "--name", "hypr")
    e.cli_json("snap", "center", "--window", g)
    e.cli_json("snap", "top-left-quarter", "--window", q)
    rep = e.cli_json("restore", "--snapshot", "hypr")
    check("H13", "snapshot restore puts Hyprland windows back exactly", rep["state"] == "completed" and rect_eq(e.frame(g), want_g, 2) and rect_eq(e.frame(q), want_q, 2))

    # UI panel through the panel shortcut
    tiled_before = {c["address"]: (c["at"], c["size"]) for c in h.j("clients") if not c["floating"]}
    e.call("activate_window", {"window": g})
    h.keys("ctrl", "alt", "g")
    panel = e.wait_for(lambda: next((w for w in e.windows(True) if w["title"].startswith("Fractile panel")), None), 15.0, 0.3)
    focused = panel and e.wait_for(lambda: e.call("active_window") == panel["id"], 3.0)
    pc = panel and h.client(panel["id"])
    ok = bool(panel) and focused and inside(panel["frame"], wa) and panel["is_own"] and pc and pc["floating"]
    check("H14", "panel shortcut starts the Wayland UI; the panel floats (Fractile's own window rule), is focused, inside the work area and flagged own",
          ok, f"{panel and panel['frame']} floating={pc and pc['floating']}")
    listed = {w["id"] for w in e.windows()}
    own = [w["id"] for w in e.windows(True) if w["is_own"]]
    check("H15", "Fractile UI windows are never offered as placement targets", own and not (set(own) & listed), f"{len(own)} own")
    if panel:
        # The first arrow selects cell (0,0); then two more columns. One
        # wtype call (one virtual keyboard and keymap) for the sequence.
        time.sleep(0.5)
        subprocess.run(["wtype", "-k", "Right", "-k", "Right", "-k", "Right", "-M", "shift", "-k", "Right", "-k", "Right", "-k", "Down", "-k", "Down",
                        "-m", "shift", "-k", "Return"], env=dict(os.environ, WAYLAND_DISPLAY=h.display), timeout=10)
        want = e.call("compute_rect", {"grid": {"cols": 6, "rows": 6}, "selection": {"col0": 2, "row0": 0, "col1": 4, "row1": 2}})
        got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want, 2) else None)(e.frame(g)), 4.0) or e.frame(g)
        gone = e.wait_for(lambda: not any(w["title"].startswith("Fractile panel") and w["frame"]["w"] > 0 for w in e.windows(True)
                                          if (h.client(w["id"]) or {}).get("mapped", True)), 3.0)
        back = e.wait_for(lambda: e.call("active_window") == g, 3.0)
        if not back:
            print("after the panel closed: active", e.call("active_window"), "target", g)
            print([(c["address"], c["title"], c["class"], c["mapped"], c["hidden"], c["floating"], c["at"], c["size"], c["focusHistoryID"]) for c in h.j("clients")])
            print("\n".join(ln for ln in (e.base / "daemon.out").read_text().splitlines()[-6:]))
        check("H22", "panel keyboard selection (arrows, Shift+arrows, Enter) places the window; the panel closes and focus returns",
              rect_eq(got, want, 2) and gone and back, f"want {want} got {got}; closed {bool(gone)} focus back {bool(back)}")
        if not gone:
            e.call("hide_panel")

    # Config reload drops runtime binds; Fractile adds them again.
    # A marker bind added at runtime shows the reload really dropped runtime
    # binds; Fractile's must come back.
    n_before = len(h.ours())
    h.dispatch('(function() hl.bind("CTRL + ALT + 9", hl.dsp.event("marker"), { description = "reload marker" }) end)')
    marked = any(b.get("description") == "reload marker" for b in h.j("binds"))
    h.ctl("reload")
    dropped = e.wait_for(lambda: not any(b.get("description") == "reload marker" for b in h.j("binds")), 5.0, 0.2)
    gone = marked and dropped and e.wait_for(lambda: len(h.ours()) == n_before, 5.0, 0.2)
    # The panel of the check before closes and gives focus back to its
    # window; wait for that before focusing the window the keys are for.
    e.wait_for(lambda: not any(w.get("is_own") for w in e.windows(True)), 5.0, 0.2)
    pre = e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", q)
    e.call("activate_window", {"window": q})
    e.wait_for(lambda: e.call("active_window") == q, 3.0)
    h.keys("ctrl", "alt", "1")
    placed = e.wait_for(lambda: rect_eq(e.frame(q), target, 3), 4.0, 0.05)
    check("H16", "after `hyprctl reload` the shortcuts are bound again and still work", gone and placed,
          f"marker bind dropped by the reload: {marked and dropped}; Fractile binds {n_before} -> {len(h.ours())}"
          + ("" if placed else f"; frame {e.frame(q)} target {target}; before the keys {pre.get('observed')} for {pre.get('requested')} ({pre.get('attempts')} attempts); log: {e.log_tail(8)}"))

    # Second monitor (a headless output) and moving a window to it.
    mk = h.ctl("output", "create", "headless", "FR-2")
    h.dispatch('(function() hl.monitor({ output = "FR-2", mode = "1280x1024@60", position = "1600x0", scale = 1 }) end)')
    # The monitor rule (position 1600x0) applies a moment after the output appears.
    two = e.wait_for(lambda: (lambda os_: len(os_) == 2 and any(x["rect"]["x"] == 1600 for x in os_))(e.cli_json("list-outputs")), 5.0, 0.2)
    moved = False
    detail = mk.stdout.strip()[:80]
    if two:
        outs = e.cli_json("list-outputs")
        other = next(x for x in outs if x["stable_id"] != o["stable_id"])
        e.cli_json("move-output", "next", "--window", g)
        moved = e.wait_for(lambda: (lambda w: w and w.get("output_id") == other["stable_id"] and inside(w["frame"], other["rect"]))(
            next((w for w in e.windows(True) if w["id"] == g), None)), 4.0, 0.1)
        hc = h.client(g) or {}
        detail = f"second output {other['name']} {other['rect']}; window {e.frame(g)} hypr monitor={hc.get('monitor')} ws={hc.get('workspace')}"
        e.cli_json("move-output", "next", "--window", g)
    check("H17", "a second monitor is listed, and a window moves onto it", two and moved, detail)
    h.ctl("output", "remove", "FR-2")

    # Orderly exit removes the binds; a crash leaves them, and the next
    # service replaces them (found without HYPRLAND_INSTANCE_SIGNATURE).
    e.stop_daemon()
    left = h.ours()
    check("H18", "on exit Fractile removes its Hyprland binds", not left, f"{len(left)} left")
    e.start_daemon()
    e.wait_for(lambda: len(h.ours()) >= 2, 5.0)
    os.kill(e.daemon.pid, signal.SIGKILL)
    e.daemon.wait(5)
    stale = len(h.ours())
    env2 = dict(e.env)
    env2.pop("HYPRLAND_INSTANCE_SIGNATURE")
    e.start_daemon(env2)
    ctl = e.wait_for(lambda: e.status()["window_control"], 10.0)
    e.wait_for(lambda: len(h.ours()) >= 2, 5.0)
    ours = h.ours()
    dup = len(ours) != len({b["description"] for b in ours})
    e.call("activate_window", {"window": q})
    e.wait_for(lambda: e.call("active_window") == q, 3.0)
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", q)
    # The service answers IPC before it has subscribed to Hyprland's event
    # socket; a key pressed in that moment is not seen.
    time.sleep(1.0)
    h.keys("ctrl", "alt", "1")
    placed = e.wait_for(lambda: rect_eq(e.frame(q), target, 3), 4.0, 0.05)
    check("H19", "after a crash, a new service (without HYPRLAND_INSTANCE_SIGNATURE) finds Hyprland and replaces the stale binds",
          stale >= 2 and ctl and not dup and placed, f"stale {stale}, now {len(ours)} dup={dup} control={ctl} placed={placed} frame={e.frame(q)}")

    tb = [ln for ln in (e.ui_log.read_text().splitlines() if e.ui_log.exists() else []) if "Traceback" in ln]
    check("H20", "UI logged no Python errors", not tb, "; ".join(tb[:2]))

    # Hyprland exits (log-out): the service saves and exits.
    h.proc.terminate()
    h.proc.wait(15)
    gone = e.wait_for(lambda: e.daemon.poll() is not None, 15.0, 0.2)
    log = (e.base / "daemon.out").read_text()
    check("H21", "when Hyprland exits the service saves the last seen layout and exits", gone and "display closed" in log, e.log_tail(3).replace("\n", " | ")[-200:])


def main():
    h = Hypr()
    e = None
    try:
        h.start()
        e = Env(h)
        run_all(h, e)
    except Exception as ex:
        check("HXX", "suite crashed", False, repr(ex))
    finally:
        if e:
            for pr in e.procs:
                pr.terminate()
            e.stop_daemon()
        if h.proc and h.proc.poll() is None:
            h.proc.terminate()
    hv = subprocess.run(["Hyprland", "--version"], capture_output=True, text=True).stdout.strip().splitlines()
    ev = {"suite": "e2e-hyprland", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"{hv[0] if hv else 'Hyprland'} nested in kwin_wayland --virtual, archlinux container (render device: {os.environ.get('FRACTILE_TEST_GPU', 'none')}); keys pressed with wtype",
          "results": results}
    out = Path(os.environ.get("EVIDENCE", "/tmp/e2e-hyprland.json"))
    out.write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    if failed and e and (e.base / "daemon.out").exists():
        print("== daemon log (warnings)")
        print("\n".join([ln for ln in (e.base / "daemon.out").read_text().splitlines() if " Warn " in ln or " Error " in ln][-25:]))
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
