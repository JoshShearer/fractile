#!/usr/bin/env python3
"""End-to-end tests on GNOME Shell *Wayland* through the Fractile GNOME Shell
extension.

Starts an isolated headless gnome-shell --wayland (no Xwayland, private D-Bus,
private XDG_RUNTIME_DIR, virtual monitor; tests/integration/headless-gnome-
wayland.sh) with the in-tree extension enabled, an isolated fractiled, native
Wayland GTK4 and Qt test windows and the real Fractile UI. Shortcuts are
pressed with real key events injected by the *test harness* through Mutter's
RemoteDesktop API on the private bus (tests/tools/wlkeys.py); Fractile itself
never injects input.

Usage: tests/integration/e2e_gnome_wayland.py [--keep] [--evidence FILE]
Exit code 0 only if every check passed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))
from fractile_ui.ipc import Client  # noqa: E402

BIN = Path(os.environ.get("FRACTILE_BIN_DIR", ROOT / "target" / "debug"))
TESTWIN = ROOT / "tests" / "tools" / "testwin.py"
WLKEYS = ROOT / "tests" / "tools" / "wlkeys.py"
HEADLESS = ROOT / "tests" / "integration" / "headless-gnome-wayland.sh"
EXT_UUID = "fractile@fractile.io"
NAME = f"fwl-e2e-{os.getpid()}"

results: list[dict] = []


def check(tid: str, name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    results.append({"id": tid, "name": name, "ok": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {tid} {name}" + (f" :: {detail}" if detail else ""), flush=True)
    return ok


def rect_eq(a, b, tol=0):
    return bool(a and b and all(abs(a[k] - b[k]) <= tol for k in ("x", "y", "w", "h")))


class Env:
    def __init__(self, monitors="1600x1200"):
        self.base = Path(tempfile.mkdtemp(prefix="fractile-wl-e2e-"))
        ext_home = self.base / "shell-data"
        dst = ext_home / "gnome-shell" / "extensions" / EXT_UUID
        shutil.copytree(ROOT / "adapters" / "gnome-shell" / EXT_UUID, dst)
        env = dict(os.environ)
        env["FRACTILE_TEST_DATA_HOME"] = str(ext_home)
        env.pop("FRACTILE_RUNTIME_DIR", None)
        st = subprocess.run(["bash", str(HEADLESS), "start", NAME, monitors, EXT_UUID], env=env, capture_output=True, text=True)
        if st.returncode != 0:
            raise RuntimeError(f"headless shell failed: {st.stderr}")
        info = dict(kv.split("=", 1) for kv in st.stdout.strip().splitlines()[-1].split())
        self.shell_state = Path(info["STATE"])
        self.env = dict(os.environ)
        for k in ("DISPLAY", "FRACTILE_RUNTIME_DIR", "XAUTHORITY"):
            self.env.pop(k, None)
        self.env.update(
            XDG_RUNTIME_DIR=info["XDG_RUNTIME_DIR"],
            WAYLAND_DISPLAY=info["WAYLAND_DISPLAY"],
            DBUS_SESSION_BUS_ADDRESS=info["DBUS_SESSION_BUS_ADDRESS"],
            XDG_SESSION_TYPE="wayland",
            XDG_CURRENT_DESKTOP="GNOME",
            FRACTILE_DATA_DIR=str(self.base / "data"),
            FRACTILE_CONFIG=str(self.base / "config.toml"),
            XDG_STATE_HOME=str(self.base / "state"),
            XDG_DATA_HOME=str(self.base / "xdgdata"),
            XDG_CONFIG_HOME=str(self.base / "xdgconfig"),
            # logind is the fake on the private bus (tests/tools/fake_logind.py)
            FRACTILE_TEST_LOGIND_BUS=info["DBUS_SESSION_BUS_ADDRESS"],
            FRACTILE_AUTOSTART_MODE="none",
            FRACTILE_NO_SCOPE="1",
            GSETTINGS_BACKEND="memory",
            QT_QPA_PLATFORM="wayland",
            GDK_BACKEND="wayland",
        )
        self.sock = Path(info["XDG_RUNTIME_DIR"]) / "fractile" / "fractile.sock"
        self.ui_log = self.base / "ui.log"
        wrap = self.base / "ui-wrap.sh"
        wrap.write_text(f'#!/bin/sh\nexec python3 {ROOT / "ui" / "fractile-ui"} "$@" >>{self.ui_log} 2>&1\n')
        wrap.chmod(0o755)
        self.env["FRACTILE_UI"] = str(wrap)
        self.procs: list[subprocess.Popen] = []
        self.daemon: subprocess.Popen | None = None
        fl = subprocess.Popen([sys.executable, str(ROOT / "tests" / "tools" / "fake_logind.py")], env=self.env, stdout=subprocess.PIPE, text=True)
        if fl.stdout.readline().strip() != "ready":
            raise RuntimeError("fake logind did not start")
        self.procs.append(fl)
        apps = self.base / "xdgdata" / "applications"
        apps.mkdir(parents=True)

    # -- plumbing
    def run(self, *args, timeout=120) -> subprocess.CompletedProcess:
        return subprocess.run([str(BIN / "fractile"), *args], env=self.env, capture_output=True, text=True, timeout=timeout)

    def cli_json(self, *args):
        p = self.run("--json", *args)
        if p.returncode != 0:
            raise RuntimeError(f"fractile {' '.join(args)} -> {p.returncode}: {p.stderr}")
        return json.loads(p.stdout)

    def client(self) -> Client:
        return Client("e2e-wl", self.sock)

    def start_daemon(self):
        log = open(self.base / "daemon.out", "a")
        self.daemon = subprocess.Popen([str(BIN / "fractiled"), "--verbose"], env=self.env, stdout=log, stderr=log)
        for _ in range(100):
            if self.run("status").returncode == 0:
                return
            time.sleep(0.1)
        raise RuntimeError("daemon did not come up")

    def stop_daemon(self, sig=signal.SIGTERM):
        if self.daemon and self.daemon.poll() is None:
            self.daemon.send_signal(sig)
            try:
                self.daemon.wait(10)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
        self.daemon = None

    def window(self, toolkit, app_id, title, *extra):
        p = subprocess.Popen([sys.executable, str(TESTWIN), "--toolkit", toolkit, "--app-id", app_id, "--title", title, *extra],
                             env=self.env, stdout=subprocess.DEVNULL, stderr=open(self.base / f"win-{app_id}.log", "a"))
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

    def wait_window(self, pred, timeout=10.0):
        return self.wait_for(lambda: next((w for w in self.windows() if pred(w)), None), timeout, 0.2)

    def frame(self, wid):
        return next((w["frame"] for w in self.windows(True) if w["id"] == wid), None)

    def active(self):
        c = self.client()
        try:
            return c.call("active_window")
        finally:
            c.close()

    def activate(self, wid):
        c = self.client()
        try:
            c.call("activate_window", {"window": wid})
        finally:
            c.close()
        self.wait_for(lambda: self.active() == wid, 3.0)

    def logind(self, path, iface, method, *args):
        """Drive the fake logind (test tool)."""
        p = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.login1", "--object-path", path, "--method", f"{iface}.{method}", *args],
                           env=self.env, capture_output=True, text=True, timeout=10)
        return p.stdout.strip()

    def keys(self, *combos):
        subprocess.run([sys.executable, str(WLKEYS), *combos], env=self.env, check=True, timeout=30)

    def gext(self, verb, uuid):
        # Straight to the shell: the gnome-extensions CLI goes through a
        # D-Bus-activated helper, and the test bus activates nothing.
        method = {"enable": "EnableExtension", "disable": "DisableExtension"}[verb]
        return subprocess.run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path", "/org/gnome/Shell",
                               "--method", f"org.gnome.Shell.Extensions.{method}", uuid], env=self.env, capture_output=True, text=True, timeout=20)

    def status(self):
        return self.cli_json("status")

    def ui_pids(self):
        pids = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                cmd = (d / "cmdline").read_bytes().split(b"\0")
                env = (d / "environ").read_bytes()
            except OSError:
                continue
            if any(c.endswith(b"fractile-ui") for c in cmd) and f"FRACTILE_UI={self.env['FRACTILE_UI']}".encode() in env:
                pids.append(int(d.name))
        return pids

    def cleanup(self, keep=False):
        for pid in self.ui_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        self.stop_daemon()
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        if not keep:
            subprocess.run(["bash", str(HEADLESS), "stop", NAME])


def expected(e: Env, cols, rows, c0, r0, c1, r1, output=None):
    c = e.client()
    try:
        p = {"grid": {"cols": cols, "rows": rows}, "selection": {"col0": c0, "row0": r0, "col1": c1, "row1": r1}}
        if output:
            p["output"] = output
        return c.call("compute_rect", p)
    finally:
        c.close()


def run_all(e: Env) -> None:
    e.start_daemon()
    st = e.wait_for(lambda: (lambda s: s if s["window_control"] else None)(e.status()), 10.0) or e.status()
    caps = e.cli_json("diagnose").get("capabilities", {})
    check("W01", "extension attaches over the private socket; window control available on Wayland",
          st["adapter"] == "gnome" and st["window_control"] and st["session_type"] == "wayland" and os.getuid() != 0,
          str(caps.get("compositor_version")))

    # Only the compositor may act as the integration.
    s = socket.socket(socket.AF_UNIX)
    s.connect(str(e.sock))
    f = s.makefile("rw")
    f.write(json.dumps({"id": 1, "method": "hello", "params": {"protocol": 1, "client": "impostor"}}) + "\n")
    f.flush()
    f.readline()
    f.write(json.dumps({"id": 2, "method": "bridge_attach", "params": {"kind": "gnome", "api": 1, "operations": {}}}) + "\n")
    f.flush()
    resp = json.loads(f.readline())
    f.write(json.dumps({"id": 3, "method": "bridge_event", "params": {"type": "hotkey", "binding_id": "panel", "timestamp": 0}}) + "\n")
    f.flush()
    resp2 = json.loads(f.readline())
    s.close()
    check("SEC5", "a non-compositor process cannot attach as the integration or inject events",
          (resp.get("error") or {}).get("code") == "denied" and (resp2.get("error") or {}).get("code") == "denied",
          f"attach: {(resp.get('error') or {}).get('message', resp)}; event: {(resp2.get('error') or {}).get('message', resp2)}")

    e.window("gtk4", "org.fractile.TestGtk", "Gtk window")
    e.window("qt", "org.fractile.TestQt", "Qt window")
    gtk = e.wait_window(lambda w: w.get("app_id") == "org.fractile.TestGtk")
    qt = e.wait_window(lambda w: w.get("title") == "Qt window")
    check("W02", "native Wayland GTK4 and Qt windows are listed with app id and pid",
          bool(gtk and qt) and gtk.get("pid") and qt.get("app_id") == "org.fractile.TestQt",
          f"{gtk and gtk['id']} {qt and qt['id']}")
    if not (gtk and qt):
        return
    g, q = gtk["id"], qt["id"]

    outs = e.cli_json("list-outputs")
    o = outs[0]
    check("W03", "outputs have a stable id and a work area that excludes the top bar",
          len(outs) == 1 and o["stable_id"] and o["work_area"]["y"] > o["rect"]["y"] and o["work_area"]["h"] < o["rect"]["h"],
          f"{o['stable_id']} work_area={o['work_area']}")
    wa = o["work_area"]

    ok_all, detail = True, []
    for grid, cells in [("6x6", "0,0,2,5"), ("4x4", "1,1,2,3"), ("3x2", "2,0,2,1"), ("10x10", "0,0,9,9")]:
        out = e.cli_json("place", "--grid", grid, "--cells", cells, "--window", g)
        ok = out["within_tolerance"] and rect_eq(out["requested"], e.frame(g), 2)
        ok_all &= ok
        detail.append(f"{grid}:{cells} err={out['max_error_px']}px")
    check("W04", "grid placements land on the requested rectangle (GTK4, client-side decorations)", ok_all, "; ".join(detail))
    out = e.cli_json("place", "--grid", "6x6", "--cells", "3,0,5,2", "--window", q)
    check("W05", "grid placement on a Qt Wayland window", out["within_tolerance"] and rect_eq(out["requested"], e.frame(q), 2), f"err={out['max_error_px']}px")
    full = e.cli_json("place", "--grid", "1x1", "--cells", "0,0,0,0", "--window", g)
    check("W06", "1x1 selection fills the work area exactly", rect_eq(e.frame(g), wa, 2), f"{full['observed']} vs {wa}")
    before = e.frame(g)
    e.cli_json("snap", "right-half", "--window", g)
    e.cli_json("undo")
    check("W07", "undo restores the previous geometry", rect_eq(e.frame(g), before, 2))

    # maximized window: placement unmaximizes first
    c = e.client()
    c.call("set_state", {"window": g, "state": {"maximized": True}})
    c.close()
    e.wait_for(lambda: next((w["state"]["maximized"] for w in e.windows(True) if w["id"] == g), False), 3.0)
    out = e.cli_json("snap", "left-half", "--window", g)
    gw = next(w for w in e.windows(True) if w["id"] == g)
    check("W08", "placing a maximized window unmaximizes it and lands exactly", not gw["state"]["maximized"] and rect_eq(gw["frame"], out["requested"], 2),
          f"maximized={gw['state']['maximized']} frame={gw['frame']}")

    # global shortcut through a real key event
    c = e.client()
    cfg = c.call("get_config")["config"]
    cfg["general"]["first_run_complete"] = True
    cfg["general"]["panel_shortcut"] = "Ctrl+Alt+G"
    cfg["shortcuts"].append({"id": "t-left-third", "name": "Left third", "scope": "global", "accelerator": "Ctrl+Alt+1", "cycle_monitors": True,
                             "action": {"type": "place", "grid": {"cols": 3, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}})
    r = c.call("set_config", {"config": cfg})
    c.close()
    e.activate(q)
    target = {"x": wa["x"], "y": wa["y"], "w": wa["w"] // 3, "h": wa["h"]}
    act_before = e.active()
    t0 = time.time()
    e.keys("ctrl+alt+1")
    placed = e.wait_for(lambda: rect_eq(e.frame(q), target, 3), 3.0, 0.05)
    check("W09", "global shortcut (grabbed by GNOME Shell) places the focused window", placed and not r["hotkey_errors"],
          f"{round((time.time() - t0) * 1000)} ms incl. key injection" if placed else f"frame {e.frame(q)} want {target}; errors {r['hotkey_errors']}; active before {act_before} (q={q}) after {e.active()}")

    # workspaces
    c = e.client()
    wss = c.call("list_workspaces")
    c.close()
    if len(wss) >= 2:
        c = e.client()
        c.call("move_to_workspace", {"window": q, "workspace": 1})
        c.close()
        on1 = e.wait_for(lambda: next((w.get("workspace") for w in e.windows(True) if w["id"] == q), None) == 1, 3.0)
        c = e.client()
        c.call("move_to_workspace", {"window": q, "workspace": 0})
        c.close()
        check("W10", "workspaces are listed and a window can be moved between them", on1, f"{len(wss)} workspaces")
    else:
        check("W10", "workspaces are listed and a window can be moved between them", False, f"only {len(wss)} workspace(s)")

    # snapshot / restore
    e.cli_json("place", "--grid", "2x2", "--cells", "0,0,0,1", "--window", g)
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", q)
    want_g, want_q = e.frame(g), e.frame(q)
    e.cli_json("save", "--name", "wl")
    e.cli_json("snap", "center", "--window", g)
    e.cli_json("snap", "top-left-quarter", "--window", q)
    rep = e.cli_json("restore", "--snapshot", "wl")
    check("W11", "snapshot restore puts Wayland windows back exactly", rep["state"] == "completed" and rect_eq(e.frame(g), want_g, 2) and rect_eq(e.frame(q), want_q, 2),
          f"{rep['report']['duration_ms']} ms")

    # The UI on Wayland: panel opened by the shortcut, positioned and
    # focused through the extension, driven with the keyboard.
    e.activate(g)
    t0 = time.time()
    e.keys("ctrl+alt+g")
    panel = e.wait_for(lambda: next((w for w in e.windows(True) if w["title"].startswith("Fractile panel")), None), 10.0, 0.2)
    dt = time.time() - t0
    st = e.status()
    focused = panel and e.wait_for(lambda: e.active() == panel["id"], 3.0)
    inside = panel and all(panel["frame"][k] >= wa[k] for k in ("x", "y")) and panel["frame"]["x"] + panel["frame"]["w"] <= wa["x"] + wa["w"] and panel["frame"]["y"] + panel["frame"]["h"] <= wa["y"] + wa["h"]
    check("W12", "shortcut starts the Wayland UI; the panel is shown, focused and inside the work area",
          bool(panel) and st.get("ui_attached") and focused and inside and panel["is_own"],
          f"{dt:.1f}s incl. UI start; panel {panel and panel['frame']} own={panel and panel['is_own']}")
    if panel:
        e.keys("right", "right", "right", "shift+right", "shift+right", "shift+down", "shift+down", "return")
        want = expected(e, 6, 6, 2, 0, 4, 2)
        got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want, 2) else None)(e.frame(g)), 4.0) or e.frame(g)
        check("W13", "panel keyboard selection (arrows, Shift+arrows, Enter) places the window", rect_eq(got, want, 2), f"want {want} got {got}")
        gone = e.wait_for(lambda: not any(w["title"].startswith("Fractile panel") and not w["state"]["minimized"] for w in e.windows(True)), 3.0)
        check("W14", "panel closes after placing and focus returns to the window", gone and e.wait_for(lambda: e.active() == g, 3.0), f"active {e.active()}")
        listed = {w["id"] for w in e.windows()}
        own = [w["id"] for w in e.windows(True) if w["is_own"]]
        check("W15", "Fractile UI windows are never offered as placement targets", not (set(own) & listed), f"{len(own)} own window(s)")
    else:
        for t in ("W13", "W14", "W15"):
            check(t, "panel-dependent check", False, "panel never appeared")

    # integration disabled at runtime: honest status, then recovery
    e.gext("disable", EXT_UUID)
    off = e.wait_for(lambda: not e.status()["window_control"], 5.0)
    p = e.run("place", "--grid", "2x2", "--cells", "0,0,0,0", "--window", g)
    e.gext("enable", EXT_UUID)
    back = e.wait_for(lambda: e.status()["window_control"], 10.0)
    time.sleep(0.5)
    e.activate(q)
    e.keys("ctrl+alt+1")
    hk = e.wait_for(lambda: rect_eq(e.frame(q), target, 3), 3.0, 0.05)
    check("W16", "extension disabled: Fractile reports no window control and refuses; re-enabled: control and shortcuts return",
          off and p.returncode != 0 and back and hk, f"place exit {p.returncode}: {p.stderr.strip()[:80]}")

    # daemon restart: the extension reconnects on its own
    e.stop_daemon()
    e.start_daemon()
    back = e.wait_for(lambda: e.status()["window_control"], 10.0)
    check("W17", "daemon restart: the extension reconnects without user action", back)

    # placement rule on window creation (positive control for W21)
    c = e.client()
    cfg = c.call("get_config")["config"]
    cfg["rules_enabled"] = True
    cfg["rules"] = [{"id": "r-late", "name": "Late apps right", "trigger": "on_create", "matcher": {"app_id": "org.fractile.Late"},
                     "placement": {"kind": "snap", "action": "right-half"}}]
    c.call("set_config", {"config": cfg})
    c.close()
    right = expected(e, 2, 1, 1, 0, 1, 0)
    e.window("gtk4", "org.fractile.Late", "Late one")
    late = e.wait_window(lambda w: w["app_id"] == "org.fractile.Late", 15.0)
    ok = late and e.wait_for(lambda: rect_eq(e.frame(late["id"]), right, 2), 6.0)
    check("W20", "an on-create rule places a new matching window", ok, f"{late and e.frame(late['id'])} want {right}")

    # session locked: Fractile moves, activates and restores nothing
    # The lock screen sets logind's LockedHint on real sessions (gnome-shell
    # and KDE's locker both do); the headless shell has no lock screen, so
    # the fake logind sets it.
    e.logind("/org/freedesktop/login1/session/test", "org.freedesktop.login1.Session", "SetLockedHint", "true")
    locked = e.wait_for(lambda: e.status().get("locked"), 8.0)
    e.window("qt", "org.fractile.Late", "Late two")  # GTK would hand off to the running GApplication
    late2 = e.wait_for(lambda: next((w for w in e.windows(True) if w["title"] == "Late two"), None), 15.0)
    time.sleep(2.0)
    rule_held = late2 and not rect_eq(e.frame(late2["id"]), right, 2)
    rp = e.run("restore", "--snapshot", "wl")
    before = e.frame(q)
    e.keys("ctrl+alt+g")
    time.sleep(1.0)
    no_panel = not any(w["title"].startswith("Fractile panel") and not w["state"]["minimized"] for w in e.windows(True))
    check("W21", "session locked: status says so, on-create rules and restores do not run, shortcuts do nothing",
          locked and rule_held and rp.returncode != 0 and "locked" in rp.stderr and no_panel and rect_eq(e.frame(q), before),
          f"locked={locked} rule_held={bool(rule_held)} restore exit {rp.returncode}: {rp.stderr.strip()[:90]}")

    # orderly shutdown announced by logind: snapshot written, delay lock released
    e.logind("/org/freedesktop/login1/session/test", "org.freedesktop.login1.Session", "SetLockedHint", "false")
    unlocked = e.wait_for(lambda: not e.status().get("locked"), 5.0)
    e.run("config", "set", "session.policy", '"last_clean"')
    held = e.logind("/", "io.fractile.test.FakeLogind", "InhibitorCount")
    before_ids = {x["id"] for x in e.cli_json("list-snapshots")}
    e.logind("/", "io.fractile.test.FakeLogind", "EmitPrepareForShutdown", "true")
    snap = e.wait_for(lambda: next((x for x in e.cli_json("list-snapshots") if x["id"] not in before_ids and x["mode"] == "shutdown"), None), 8.0)
    released = e.wait_for(lambda: e.logind("/", "io.fractile.test.FakeLogind", "InhibitorCount") == "(uint32 0,)", 5.0)
    check("W22", "logind PrepareForShutdown: a shutdown snapshot is written and the delay inhibitor released",
          unlocked and held == "(uint32 1,)" and snap and released, f"inhibitors before {held}; snapshot {snap and snap['id']} ({snap and snap['window_count']} windows)")
    e.run("config", "set", "session.policy", '"manual"')

    # T19: an integration speaking another adapter API (after a shell or
    # Fractile upgrade) is refused, and status names the mismatch and a fix
    e.gext("disable", EXT_UUID)
    detached = e.wait_for(lambda: not e.status()["window_control"], 8.0)
    c = e.client()
    try:
        c.call("bridge_attach", {"kind": "gnome", "api": 999, "operations": {}})
        refused = ""
    except Exception as ex:  # noqa: BLE001 - the refusal is the expected outcome
        refused = str(ex)
    c.close()
    st = e.status()
    human = e.run("status").stdout
    reported = "version mismatch" in (st["integration"].get("rejected") or "") and "To fix:" in human and not st["window_control"]
    e.gext("enable", EXT_UUID)
    back = e.wait_for(lambda: e.status()["window_control"], 10.0)
    cleared = back and "rejected" not in e.status()["integration"]
    check("W23", "an integration with a mismatched adapter API is refused; status reports the mismatch and how to fix it; the right version reattaches",
          detached and "version mismatch" in refused and reported and cleared,
          f"refused: {refused[:110]}; status line: {next((ln.strip() for ln in human.splitlines() if 'refused' in ln), '')[:90]}")

    tb = [l for l in (e.ui_log.read_text().splitlines() if e.ui_log.exists() else []) if "Traceback" in l or "Error" in l and "qt.qpa" not in l]
    check("W18", "UI logged no Python errors during the run", not tb, "; ".join(tb[:3]))
    log = (e.shell_state / "shell.log").read_text(errors="replace")
    js = [l for l in log.splitlines() if "fractile" in l.lower() and ("error" in l.lower() or "exception" in l.lower())]
    check("W19", "GNOME Shell logged no errors from the extension", not js, "; ".join(js[:3]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "e2e-gnome-wayland.json"))
    a = ap.parse_args()
    e = Env()
    try:
        run_all(e)
    except Exception as ex:  # report, don't hide
        check("WXX", "suite crashed", False, repr(ex))
    finally:
        if any(not r["ok"] for r in results) and (e.base / "daemon.out").exists():
            print("== daemon log (rules, warnings)")
            print("\n".join([ln for ln in (e.base / "daemon.out").read_text().splitlines() if " Warn " in ln or " Error " in ln or "rule" in ln][-25:]))
        e.cleanup(a.keep)
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "e2e-gnome-wayland", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"headless {gs} --wayland --no-x11, virtual 1600x1200 monitor, Fractile extension {EXT_UUID}", "results": results}
    Path(a.evidence).parent.mkdir(parents=True, exist_ok=True)
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
