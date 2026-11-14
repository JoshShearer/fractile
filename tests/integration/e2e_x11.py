#!/usr/bin/env python3
"""End-to-end tests against a real window manager (nested GNOME Shell on X11).

Starts an isolated Xephyr + gnome-shell (tests/integration/nested-gnome-x11.sh),
an isolated fractiled (private runtime/data/config dirs), real GTK4/Qt test
windows, and drives everything through the real `fractile` CLI, the IPC socket
and real X key events (xdotool). Nothing touches the user's own session.

Usage: tests/integration/e2e_x11.py [--display 17] [--keep] [--evidence FILE]
Exit code 0 only if every check passed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
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

results: list[dict] = []


def check(tid: str, name: str, ok: bool, detail: str = "") -> bool:
    results.append({"id": tid, "name": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {tid} {name}" + (f" :: {detail}" if detail else ""), flush=True)
    return ok


class Env:
    def __init__(self, display: int):
        self.display = display
        self.base = Path(tempfile.mkdtemp(prefix="fractile-e2e-"))
        # Unix socket paths must be short: keep the runtime dir under XDG_RUNTIME_DIR.
        rt = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / f"fe2e-{os.getpid()}"
        self.env = dict(os.environ)
        self.env.update(
            DISPLAY=f":{display}",
            XDG_SESSION_TYPE="x11",
            XDG_CURRENT_DESKTOP="KDE" if os.environ.get("FRACTILE_TEST_WM") == "kwin" else "GNOME",
            FRACTILE_RUNTIME_DIR=str(rt),
            FRACTILE_DATA_DIR=str(self.base / "data"),
            FRACTILE_CONFIG=str(self.base / "config.toml"),
            XDG_STATE_HOME=str(self.base / "state"),
            XDG_DATA_HOME=str(self.base / "xdgdata"),
            FRACTILE_NO_LOGIND="1",
            FRACTILE_AUTOSTART_MODE="none",
            FRACTILE_UI="/bin/true",
            FRACTILE_NO_SCOPE="1",
        )
        self.env.pop("WAYLAND_DISPLAY", None)
        os.environ.update({k: self.env[k] for k in ("FRACTILE_RUNTIME_DIR",)})
        self.procs: list[subprocess.Popen] = []
        self.daemon: subprocess.Popen | None = None
        apps = self.base / "xdgdata" / "applications"
        apps.mkdir(parents=True)
        (apps / "org.fractile.Launchme.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Fractile Launch Test\n"
            f"Exec=python3 {TESTWIN} --toolkit gtk4 --app-id org.fractile.Launchme --title Launchme\nTerminal=false\n"
        )
        (apps / "org.fractile.Unicode.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Rédacteur 編集 Ünïcode\n"
            f"Exec=python3 {TESTWIN} --toolkit gtk4 --app-id org.fractile.Unicode\nTerminal=false\n", encoding="utf-8"
        )

    def run(self, *args, check_rc=None, timeout=300) -> subprocess.CompletedProcess:
        p = subprocess.run([str(BIN / "fractile"), *args], env=self.env, capture_output=True, text=True, timeout=timeout)
        return p

    def cli_json(self, *args):
        p = self.run("--json", *args)
        if p.returncode != 0:
            raise RuntimeError(f"fractile {' '.join(args)} -> {p.returncode}: {p.stderr}")
        return json.loads(p.stdout)

    def start_daemon(self, *extra) -> None:
        log = open(self.base / "daemon.out", "a")
        self.daemon = subprocess.Popen([str(BIN / "fractiled"), "--verbose", *extra], env=self.env, stdout=log, stderr=log)
        for _ in range(100):
            if self.run("status").returncode == 0:
                return
            time.sleep(0.1)
        raise RuntimeError("daemon did not come up; see " + str(self.base / "daemon.out"))

    def stop_daemon(self, sig=signal.SIGTERM) -> None:
        if self.daemon and self.daemon.poll() is None:
            self.daemon.send_signal(sig)
            try:
                self.daemon.wait(10)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
        self.daemon = None

    def window(self, toolkit: str, app_id: str, title: str, *extra) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, str(TESTWIN), "--toolkit", toolkit, "--app-id", app_id, "--title", title, *extra], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def client(self) -> Client:
        return Client("e2e", Path(self.env["FRACTILE_RUNTIME_DIR"]) / "fractile.sock")

    def wait_window(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            for w in self.cli_json("list-windows"):
                if pred(w):
                    return w
            time.sleep(0.2)
        return None

    @staticmethod
    def wait_for(fn, timeout=8.0, step=0.1):
        end = time.time() + timeout
        while True:
            v = fn()
            if v or time.time() >= end:
                return v
            time.sleep(step)

    def frame(self, wid: str):
        for w in self.cli_json("list-windows", "--all"):
            if w["id"] == wid:
                return w["frame"]
        return None

    def xdo(self, *args):
        # bounded: xdotool's --sync waits forever if the WM adjusts the result
        try:
            subprocess.run(["xdotool", *args], env=self.env, check=False, capture_output=True, timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def cleanup(self):
        self.stop_daemon()
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(self.env["FRACTILE_RUNTIME_DIR"], ignore_errors=True)
        shutil.rmtree(self.base, ignore_errors=True)


def rect_eq(a, b, tol=0):
    return a and b and all(abs(a[k] - b[k]) <= tol for k in ("x", "y", "w", "h"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", type=int, default=17)
    ap.add_argument("--keep", action="store_true", help="leave the nested desktop running")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "e2e-x11.json"))
    a = ap.parse_args()

    nested = ROOT / "tests" / "integration" / "nested-gnome-x11.sh"
    st = subprocess.run(["bash", str(nested), "start", str(a.display), "1600x1200"], capture_output=True, text=True)
    if st.returncode != 0:
        print(st.stdout, st.stderr)
        return 2
    print(st.stdout.strip().splitlines()[-1])
    e = Env(a.display)
    try:
        run_all(e)
    except Exception as ex:  # noqa: BLE001  record, then still write evidence
        check("XXX", "suite crashed", False, repr(ex))
    finally:
        e.cleanup()
        if not a.keep:
            subprocess.run(["bash", str(nested), "stop", str(a.display)])
    if os.environ.get("FRACTILE_TEST_WM") == "kwin":
        # kwin_x11 --version needs an X display; ask the package manager.
        vp = subprocess.run(["dpkg-query", "-W", "-f", "${Version}", "kwin-x11"], capture_output=True, text=True)
        kv = f"kwin_x11 {vp.stdout.strip()}" if vp.returncode == 0 and vp.stdout.strip() else "kwin_x11 (version unknown)"
        envdesc = f"{os.environ.get('FRACTILE_TEST_XSERVER', 'xephyr')} with {kv} (Plasma X11 window manager, no plasmashell)"
    else:
        gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
        envdesc = f"Xephyr nested {gs} (X11), real mutter WM"
    ev = {"suite": "e2e-x11", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "environment": envdesc, "results": results}
    Path(a.evidence).parent.mkdir(parents=True, exist_ok=True)
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


def run_all(e: Env) -> None:
    e.start_daemon()
    st = e.cli_json("status")
    check("X01", "service starts unprivileged with X11 adapter and window control", st["adapter"] == "x11" and st["window_control"] and os.getuid() != 0, f"pid {st['pid']}")

    # socket permissions
    sock = Path(e.env["FRACTILE_RUNTIME_DIR"]) / "fractile.sock"
    mode = sock.stat().st_mode & 0o777
    dmode = sock.parent.stat().st_mode & 0o777
    check("SEC1", "IPC socket is 0600 in a 0700 dir", mode == 0o600 and dmode == 0o700, f"sock {oct(mode)} dir {oct(dmode)}")

    # second instance
    p = subprocess.run([str(BIN / "fractiled")], env=e.env, capture_output=True, text=True, timeout=10)
    check("X02", "second instance exits without starting twice", p.returncode == 0 and "already running" in p.stderr, p.stderr.strip())

    # IPC requires hello
    import socket as _s
    s = _s.socket(_s.AF_UNIX)
    s.connect(str(sock))
    s.sendall(b'{"id":1,"method":"list_windows","params":{}}\n')
    resp = s.recv(4096).decode()
    s.close()
    check("SEC2", "IPC rejects requests before hello", "first message must be hello" in resp)

    e.window("gtk4", "org.fractile.TestGtk", "Gtk window")
    e.window("qt", "org.fractile.TestQt", "Qt window")
    gtk = e.wait_window(lambda w: w.get("app_id") == "org.fractile.TestGtk")
    qt = e.wait_window(lambda w: w.get("title") == "Qt window")
    check("X03", "real GTK4 and Qt windows are listed", bool(gtk and qt))
    if not (gtk and qt):
        return
    g, q = gtk["id"], qt["id"]
    outs = e.cli_json("list-outputs")
    wa = outs[0]["work_area"]

    # grid placement exactness
    ok_all = True
    detail = []
    for grid, cells in [("6x6", "0,0,2,5"), ("4x4", "1,1,2,3"), ("3x2", "2,0,2,1"), ("10x10", "0,0,9,9")]:
        out = e.cli_json("place", "--grid", grid, "--cells", cells, "--window", g)
        ok = out["within_tolerance"] and rect_eq(out["requested"], out["observed"], 2)
        ok_all &= ok
        detail.append(f"{grid}:{cells} err={out['max_error_px']}px")
    check("X04", "grid placements land on the requested rectangle (GTK4)", ok_all, "; ".join(detail))
    out = e.cli_json("place", "--grid", "6x6", "--cells", "3,0,5,2", "--window", q)
    check("X05", "grid placement on a Qt (server-side decorated) window", out["within_tolerance"], f"err={out['max_error_px']}px")
    full = e.cli_json("place", "--grid", "1x1", "--cells", "0,0,0,0", "--window", g)
    check("X06", "1x1 selection fills the work area exactly (no panel overlap)", rect_eq(full["observed"], wa, 2), f"{full['observed']} vs work area {wa}")

    # snap + undo
    before = e.frame(g)
    e.cli_json("snap", "right-half", "--window", g)
    e.cli_json("undo")
    check("X07", "undo restores the previous geometry", rect_eq(e.frame(g), before, 2))

    # error exit codes
    check("X08", "invalid selection exits 7", e.run("place", "--grid", "6x6", "--cells", "0,0,9,9", "--window", g).returncode == 7)
    check("X08b", "unknown window exits 5", e.run("place", "--grid", "6x6", "--cells", "0,0,1,1", "--window", "x11:0xdead").returncode == 5)

    # global hotkeys through real X key events
    c = e.client()
    cfg = c.call("get_config")["config"]
    for sc in cfg["shortcuts"]:
        if sc["id"] == "global-undo":
            sc["accelerator"] = "Ctrl+Alt+Z"
    cfg["shortcuts"].append({"id": "t-left-third", "name": "Left third", "scope": "global", "accelerator": "Ctrl+Alt+1", "cycle_monitors": True,
                             "action": {"type": "place", "grid": {"cols": 3, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}})
    r = c.call("set_config", {"config": cfg})
    e.xdo("windowactivate", "--sync", str(int(g.split(":")[1], 16)))
    time.sleep(0.4)
    before = e.frame(g)
    t0 = time.time()
    e.xdo("key", "ctrl+alt+1")
    target = {"x": wa["x"], "y": wa["y"], "w": wa["w"] // 3, "h": wa["h"]}
    placed = None
    while time.time() - t0 < 3:
        f = e.frame(g)
        if rect_eq(f, target, 3):
            placed = time.time() - t0
            break
        time.sleep(0.05)
    check("X09", "global shortcut places the focused window (real key event)", placed is not None and not r["hotkey_errors"], f"{placed and round(placed * 1000)} ms incl. polling" if placed else f"frame {e.frame(g)} target {target}")
    e.xdo("key", "ctrl+alt+z")
    time.sleep(1.0)
    check("X10", "global undo shortcut", rect_eq(e.frame(g), before, 2))
    chk = c.call("shortcut_check", {"accelerator": "Ctrl+Alt+1"})
    check("X11", "shortcut conflict detection reports Fractile's own binding", chk["free"] is False and "Left third" in chk["reason"])

    # snapshot / restore
    e.cli_json("place", "--grid", "2x2", "--cells", "0,0,0,1", "--window", g)
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", q)
    want_g, want_q = e.frame(g), e.frame(q)
    e.cli_json("save", "--name", "e2e")
    e.cli_json("snap", "center", "--window", g)
    e.cli_json("snap", "top-left-quarter", "--window", q)
    plan = e.cli_json("restore", "--dry-run", "--snapshot", "e2e")
    acts = sorted(i["action"] for i in plan["items"])
    moved_g = e.frame(g)
    check("X12", "dry run classifies open windows and changes nothing", acts == ["already_open", "already_open"] and rect_eq(e.frame(g), moved_g), str(plan["counts"]))
    rep = e.cli_json("restore", "--snapshot", "e2e")
    check("X13", "restore puts existing windows back exactly", rep["state"] == "completed" and rect_eq(e.frame(g), want_g, 2) and rect_eq(e.frame(q), want_q, 2), f"{rep['report']['duration_ms']} ms")
    e.cli_json("undo-restore")
    check("X14", "undo-restore returns to the pre-restore layout", rect_eq(e.frame(g), moved_g, 2))

    # launching: approval gate, launch, no duplicates
    lp = subprocess.Popen(["gio", "launch", str(e.base / "xdgdata/applications/org.fractile.Launchme.desktop")], env=e.env)
    lw = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Launchme")
    if not check("X15", "launchable test app identified by desktop id", bool(lw) and lw.get("desktop_file_id") == "org.fractile.Launchme.desktop", lw and lw.get("desktop_file_source")):
        return
    e.cli_json("place", "--grid", "4x4", "--cells", "1,1,2,3", "--window", lw["id"])
    want_l = e.frame(lw["id"])
    e.cli_json("save", "--name", "with-launch")
    os.kill(lw["pid"], signal.SIGTERM)
    lp.wait()
    for _ in range(50):
        if not any(w.get("app_id") == "org.fractile.Launchme" for w in e.cli_json("list-windows")):
            break
        time.sleep(0.1)
    plan = e.cli_json("restore", "--dry-run", "--snapshot", "with-launch")
    na = [i for i in plan["items"] if i["app"] == "org.fractile.Launchme.desktop"]
    check("X16", "missing app needs approval by default (no silent launch)", na and na[0]["action"] == "needs_approval")
    p = e.run("restore", "--snapshot", "with-launch")
    still_missing = not any(w.get("app_id") == "org.fractile.Launchme" for w in e.cli_json("list-windows"))
    check("X17", "unapproved restore does not launch and exits 6 (partial)", p.returncode == 6 and still_missing)
    rep = e.cli_json("restore", "--snapshot", "with-launch", "--approve-all")
    nl = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Launchme", 5)
    check("X18", "approved restore launches the app and places it", rep["state"] == "completed" and nl and rect_eq(e.frame(nl["id"]), want_l, 2), json.dumps(rep["report"]["launches"]))
    rep2 = e.cli_json("restore", "--snapshot", "with-launch", "--approve-all")
    n = sum(1 for w in e.cli_json("list-windows") if w.get("app_id") == "org.fractile.Launchme")
    check("X19", "repeated restore never duplicates windows", n == 1 and rep2["report"]["launches"] == 0, f"{n} window(s), {rep2['report']['launches']} launches")

    # import is untrusted
    snap = e.cli_json("snapshot", "export", "with-launch")
    for w in snap["windows"]:
        w["launch_policy"] = "always"
    f = e.base / "import.json"
    f.write_text(json.dumps(snap))
    imp = e.run("snapshot", "import", str(f))
    newid = imp.stdout.split()[2] if imp.returncode == 0 else None
    pols = {w["launch_policy"] for w in e.cli_json("snapshot", "export", newid)["windows"]} if newid else set()
    check("SEC3", "imported snapshots cannot auto-launch (policies forced to ask)", pols == {"ask"}, str(pols))
    bad = json.loads(json.dumps(snap))
    bad["windows"][0]["desktop_file_id"] = "../../../bin/sh"
    f.write_text(json.dumps(bad))
    check("SEC4", "import rejects a path-like desktop id", e.run("snapshot", "import", str(f)).returncode == 7)

    # logs redaction
    logs = e.cli_json("logs", "--redacted", "--lines", "500")
    check("X20", "redacted logs contain no window titles", not any("Gtk window" in l or "Launchme\"" in l for l in logs))

    # crash during restore -> resume without duplicates
    e.cli_json("snap", "center", "--window", g)
    os.kill(nl["pid"], signal.SIGTERM)
    time.sleep(1)
    e.run("restore", "--snapshot", "with-launch", "--approve-all", "--no-wait")
    time.sleep(0.3)
    e.stop_daemon(signal.SIGKILL)
    e.start_daemon()
    time.sleep(8)
    n = sum(1 for w in e.cli_json("list-windows") if w.get("app_id") == "org.fractile.Launchme")
    runs = e.cli_json("restore-status")["recent_runs"]
    check("X21", "daemon killed mid-restore: restart resumes, no duplicate launches", n == 1, f"{n} Launchme window(s); last runs {[r.get('state') for r in runs[:3]]}")

    # SIGTERM at logout writes a snapshot when an automatic policy is on
    c = e.client()
    c.call("set_config", {"patch": {"session": {"policy": "last_clean"}}})
    count0 = len(e.cli_json("list-snapshots", "--limit", "200"))
    e.stop_daemon(signal.SIGTERM)
    e.start_daemon()
    snaps = e.cli_json("list-snapshots", "--limit", "200")
    check("X22", "orderly stop (logout) captures a snapshot", len(snaps) > count0 and any(s["mode"] == "logout" for s in snaps), f"{count0} -> {len(snaps)}")

    # broken config: service runs on defaults and never overwrites the file
    e.stop_daemon()
    cfgp = Path(e.env["FRACTILE_CONFIG"])
    cfgp.write_text("this is = = not toml [")
    e.start_daemon()
    st = e.cli_json("status")
    check("X23", "corrupt config: service starts on defaults, reports it, keeps the file", st["config_error"] is not None and cfgp.read_text().startswith("this is"), st["config_error"] and st["config_error"][:60])
    cfgp.unlink()

    # margins and gaps (DV-22, DV-23), checked with independent arithmetic
    c = e.client()
    c.call("set_config", {"patch": {"appearance": {"spacing": {"margins_enabled": True, "gaps_enabled": True}}}})
    c.close()
    e.cli_json("snap", "left-half", "--window", g)
    e.cli_json("snap", "right-half", "--window", q)
    fg, fq = e.frame(g), e.frame(q)
    m = gap = 10
    ok = (fg["x"] == wa["x"] + m and fg["y"] == wa["y"] + m and fg["h"] == wa["h"] - 2 * m and fq["y"] == wa["y"] + m and fq["h"] == wa["h"] - 2 * m
          and fq["x"] + fq["w"] == wa["x"] + wa["w"] - m and fq["x"] - (fg["x"] + fg["w"]) == gap and abs(fg["w"] - fq["w"]) <= 1)
    check("X25", "10px margins and a 10px gap: halves sit exactly inside the work area with the gap between them", ok, f"work area {wa}; left {fg}; right {fq}")
    c = e.client()
    c.call("set_config", {"patch": {"appearance": {"spacing": {"margins_enabled": False, "gaps_enabled": False}}}})
    c.close()

    # named layout: save, disturb, apply
    e.cli_json("snap", "top-left-quarter", "--window", g)
    e.cli_json("snap", "bottom-right-quarter", "--window", q)
    want_g, want_q = e.frame(g), e.frame(q)
    e.run("layout", "save", "two-up")
    e.cli_json("snap", "center", "--window", g)
    e.cli_json("snap", "center", "--window", q)
    ap = e.run("layout", "apply", "two-up", "--mode", "position_existing")
    check("X26", "a named layout puts existing windows back exactly", ap.returncode == 0 and rect_eq(e.frame(g), want_g, 2) and rect_eq(e.frame(q), want_q, 2),
          f"{e.frame(g)} {e.frame(q)}")

    # hostile layout import (T26): no command fields survive, policies demoted, nothing runs
    mark = e.base / "PWNED"
    lay = e.cli_json("layout", "export", "two-up")
    for v in lay["variants"]:
        for en in v["entries"]:
            en["exec"] = f"touch {mark}"
            en["command"] = ["sh", "-c", f"touch {mark}"]
            if en.get("launch"):
                en["launch"]["policy"] = "always"
                en["launch"]["exec"] = f"sh -c 'touch {mark}'"
    lay["name"] = "hostile"
    lay["post_apply"] = f"touch {mark}"
    f = e.base / "hostile.json"
    f.write_text(json.dumps(lay))
    imp = e.run("layout", "import", str(f))
    shown = e.cli_json("layout", "export", "hostile") if imp.returncode == 0 else {}
    text = json.dumps(shown)
    pols = {en["launch"]["policy"] for v in shown.get("variants", []) for en in v["entries"] if en.get("launch")}
    for w in e.cli_json("list-windows"):
        if w.get("app_id") == "org.fractile.Launchme":
            os.kill(w["pid"], signal.SIGTERM)
    time.sleep(1)
    ap = e.run("layout", "apply", "hostile", "--mode", "launch_missing")
    time.sleep(3)
    relaunched = any(w.get("app_id") == "org.fractile.Launchme" for w in e.cli_json("list-windows"))
    bad = json.loads(f.read_text())
    bad["name"] = "hostile2"
    for v in bad["variants"]:
        for en in v["entries"]:
            if en.get("launch"):
                en["launch"]["desktop_id"] = "../../../usr/bin/sh"
    f.write_text(json.dumps(bad))
    rej = e.run("layout", "import", str(f))
    check("SEC7", "hostile layout import: command fields dropped, launches demoted to ask, nothing executed or launched; path-like ids rejected",
          imp.returncode == 0 and "touch" not in text and pols <= {"ask"} and not mark.exists() and not relaunched and rej.returncode != 0,
          f"import {imp.returncode}, policies {pols}, apply exit {ap.returncode}, relaunched {relaunched}, path-like import exit {rej.returncode}")

    # log out and back in (new XDG_SESSION_ID): automatic restore by policy (T10, simulated relogin)
    c = e.client()
    c.call("set_config", {"patch": {"session": {"policy": "last_clean", "confirm_before_restore": False},
                                    "apps": {"org.fractile.Launchme.desktop": {"launch": "if_missing"}}}})
    c.close()
    subprocess.Popen(["gio", "launch", str(e.base / "xdgdata/applications/org.fractile.Launchme.desktop")], env=e.env)
    lw = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Launchme", 15.0)
    e.cli_json("snap", "bottom-right-quarter", "--window", lw["id"])
    e.cli_json("snap", "top-left-quarter", "--window", g)
    want_l, want_g = e.frame(lw["id"]), e.frame(g)
    e.stop_daemon(signal.SIGTERM)  # logout: snapshot
    os.kill(lw["pid"], signal.SIGTERM)  # the app does not survive the logout
    e.wait_for(lambda: not os.path.exists(f"/proc/{lw['pid']}"), 5.0)
    e.env["XDG_SESSION_ID"] = f"e2e-relogin-{os.getpid()}"
    e.start_daemon()
    e.cli_json("snap", "center", "--window", g)  # the desktop reopened it somewhere else
    back = e.wait_for(lambda: (lambda ws: ws if len(ws) == 1 and rect_eq(ws[0]["frame"], want_l, 2) else None)(
        [w for w in e.cli_json("list-windows") if w.get("app_id") == "org.fractile.Launchme"]), 30.0, 0.5)
    g_back = e.wait_for(lambda: rect_eq(e.frame(g), want_g, 2), 10.0, 0.5)
    e.stop_daemon()
    e.start_daemon()  # same session again: must not restore a second time
    time.sleep(4)
    n = sum(1 for w in e.cli_json("list-windows") if w.get("app_id") == "org.fractile.Launchme")
    check("X27", "new login with 'restore last session': missing app relaunched and both windows placed; a same-session restart adds nothing",
          bool(back) and g_back and n == 1, f"launchme {back and back[0]['frame']} want {want_l}; editor back {g_back}; instances after restart {n}")

    # log out before the login restore has started (it waits for the desktop
    # to settle), then log in again: no exit snapshot is taken of the
    # unrestored desktop, so the next login must restore the layout that
    # session was going to restore (not nothing). Logging out while a restore
    # is running is soak check P09.
    gx = str(int(g.split(":")[1], 16))
    e.cli_json("snap", "top-right-quarter", "--window", g)
    want_g = e.frame(g)
    e.stop_daemon(signal.SIGTERM)  # clean logout: snapshot with the editor top right
    e.xdo("windowmove", gx, "100", "600")
    e.env["XDG_SESSION_ID"] = f"e2e-quick-{os.getpid()}"
    e.start_daemon()
    e.stop_daemon(signal.SIGTERM)  # logged out again while the login restore waits for the desktop to settle
    skipped = "snapshot skipped: the login restore had not started yet" in (e.base / "daemon.out").read_text()
    e.xdo("windowmove", gx, "100", "600")
    e.env["XDG_SESSION_ID"] = f"e2e-after-quick-{os.getpid()}"
    e.start_daemon()
    again = e.wait_for(lambda: rect_eq(e.frame(g), want_g, 2), 15.0, 0.3)
    check("X28", "logging out during the login restore: the next login restores that layout again",
          skipped and again, f"exit snapshot of the unrestored desktop skipped: {skipped}; editor back {again} at {e.frame(g)} want {want_g}")

    # RS-01: the remaining restore policies at login
    restore_policies(e, g)

    # T20: unicode names and titles, title changes, transient dialogs
    t20(e)

    # LR-03/LR-04/LR-05/LR-07: layout modes, per-monitor variants, ordered
    # and delayed launching, profile switch on a monitor change
    layouts_lr(e, g)

    # no-WM / unsupported desktop honesty
    env2 = dict(e.env)
    env2.update(XDG_SESSION_TYPE="wayland", WAYLAND_DISPLAY="wayland-fake", XDG_CURRENT_DESKTOP="river")
    out = subprocess.run([str(BIN / "fractiled"), "--check"], env=env2, capture_output=True, text=True)
    caps = json.loads(out.stdout)["capabilities"]
    levels = {v["level"] for v in caps["operations"].values()}
    check("X24", "unknown Wayland desktop reports unsupported (no fake support)", caps["adapter"] == "none" and levels == {"unsupported"}, f"adapter={caps['adapter']} levels={levels}")


FAKE_UI = """#!/usr/bin/env python3
# Stands in for fractile-ui in the restore-policy checks: attaches as the UI
# and writes down every request the service sends it.
import json, sys, time
sys.path.insert(0, {ui!r})
from fractile_ui.ipc import Client
log = open({log!r}, "a")
c = Client("fake-ui", __import__("pathlib").Path({sock!r}))
c.on_event = lambda ev, data: (log.write(json.dumps({{"event": ev, "data": data}}) + "\\n"), log.flush())
c.start_events("ui", background="--background" in sys.argv)
while not c.closed:
    time.sleep(0.2)
"""


def restore_policies(e: Env, g: str) -> None:
    """RS-01: off, last_known, last_clean after a crash, ask and named, each
    across a simulated logout and login (new XDG_SESSION_ID). The UI is a
    stand-in that records what the service asks it to show."""
    events = e.base / "ui-events.jsonl"
    fake = e.base / "fake-ui"
    fake.write_text(FAKE_UI.format(ui=str(ROOT / "ui"), log=str(events), sock=str(Path(e.env["FRACTILE_RUNTIME_DIR"]) / "fractile.sock")))
    fake.chmod(0o755)
    gx = str(int(g.split(":")[1], 16))
    n = [0]

    def setcfg(session: dict) -> None:
        c = e.client()
        c.call("set_config", {"patch": {"session": session}})
        c.close()

    def asked() -> list[dict]:
        if not events.exists():
            return []
        out = []
        for line in events.read_text().splitlines():
            m = json.loads(line)
            if m["event"] == "show_recovery":
                out.append(m["data"])
        return out

    def relogin(sig=signal.SIGTERM) -> None:
        e.stop_daemon(sig)
        e.xdo("windowmove", gx, "100", "600")  # the next session opens it elsewhere
        if events.exists():
            events.unlink()
        n[0] += 1
        e.env["XDG_SESSION_ID"] = f"e2e-rs01-{n[0]}-{os.getpid()}"
        e.start_daemon()

    def autosaves() -> int:
        return len(re.findall(r" autosave \S+ \(\d+ windows\)", (e.base / "daemon.out").read_text()))

    def last_autosave() -> str | None:
        ids = re.findall(r" autosave (\S+) \(\d+ windows\)", (e.base / "daemon.out").read_text())
        return ids[-1] if ids else None

    def autosaved(before: int) -> bool:
        # the first autosave comes one interval (1 min) after the service starts
        return bool(e.wait_for(lambda: autosaves() > before, 110.0, 1.0))

    def restore_done(timeout=60.0) -> bool:
        return bool(e.wait_for(lambda: e.cli_json("restore-status").get("running") is None, timeout, 0.5))

    def log_tail(*words) -> str:
        lines = [ln for ln in (e.base / "daemon.out").read_text().splitlines() if any(w in ln for w in words)]
        return " | ".join(ln[-160:] for ln in lines[-6:])

    def placed(want, timeout=15.0) -> bool:
        return bool(e.wait_for(lambda: rect_eq(e.frame(g), want, 2), timeout, 0.3))

    def left_alone(want) -> bool:
        time.sleep(8)  # longer than a login restore takes here
        return not rect_eq(e.frame(g), want, 2)

    # every UI request from here on goes to the stand-in
    e.env["FRACTILE_UI"] = str(fake)
    e.stop_daemon()
    e.start_daemon()  # same session: no login restore

    # off: no snapshot at logout, nothing restored, nothing asked
    setcfg({"policy": "off"})
    e.cli_json("snap", "top-left-quarter", "--window", g)
    want = e.frame(g)
    before = len(e.cli_json("list-snapshots", "--limit", "500"))
    relogin()
    alone = left_alone(want)
    after = len(e.cli_json("list-snapshots", "--limit", "500"))
    check("RS1", "policy off: no snapshot at logout, nothing restored or offered at the next login",
          alone and after == before and not asked(), f"snapshots {before} -> {after}; editor left alone {alone}; asked {asked()}")

    # last_known after a crash (power-loss policy restore): the last autosave comes back
    setcfg({"policy": "last_known", "power_loss_policy": "restore", "autosave_interval_min": 1, "confirm_before_restore": False})
    e.cli_json("snap", "bottom-left-quarter", "--window", g)
    want = e.frame(g)
    saved = autosaved(autosaves())
    relogin(signal.SIGKILL)
    back = placed(want, 20.0)
    done = restore_done()
    log = (e.base / "daemon.out").read_text()
    check("RS2", "policy last_known: after a crash (no logout snapshot) the last autosave is restored at the next login",
          saved and back and "decision=restore unclean=true" in log, f"autosaved {saved}; editor back {back} at {e.frame(g)} want {want}; restore finished {done}")

    # last_clean after a crash: not restored silently; the user is asked
    setcfg({"policy": "last_clean"})
    e.cli_json("snap", "bottom-right-quarter", "--window", g)
    want = e.frame(g)
    saved = autosaved(autosaves())
    auto_id = last_autosave()
    relogin(signal.SIGKILL)
    got = e.wait_for(asked, 15.0, 0.3)
    alone = left_alone(want)
    a = (got or [{}])[0]
    check("RS3", "policy last_clean: after a crash nothing is restored without asking; the recovery prompt is shown for the last autosave",
          saved and alone and a.get("reason") == "unclean_shutdown" and a.get("unclean") is True and a.get("snapshot_id") == auto_id,
          f"autosaved {saved} ({auto_id}); asked {got}; editor left alone {alone}" + ("" if saved else "; log: " + log_tail("autosave", "restore", "run ")))

    # ask: a clean logout, then the login asks with the logout snapshot and restores nothing
    setcfg({"policy": "ask"})
    e.cli_json("snap", "top-right-quarter", "--window", g)
    want = e.frame(g)
    relogin()
    got = e.wait_for(asked, 15.0, 0.3)
    alone = left_alone(want)
    snaps = e.cli_json("list-snapshots", "--limit", "5")
    a = (got or [{}])[0]
    logout_id = next((s["id"] for s in snaps if s.get("mode") == "logout"), None)
    check("RS4", "policy ask: each login offers the last session (the logout snapshot) and restores nothing until the user answers",
          alone and a.get("reason") == "ask_each_login" and a.get("snapshot_id") == logout_id and logout_id is not None,
          f"asked {got}; latest logout snapshot {logout_id}; editor left alone {alone}")

    # named, no confirmation: the chosen layout is applied at login
    e.cli_json("snap", "left-half", "--window", g)
    want = e.frame(g)
    lay = e.run("layout", "save", "rs-login")
    setcfg({"policy": "named", "named_layout": "rs-login", "confirm_before_restore": False})
    relogin()
    back = placed(want, 20.0)
    check("RS5", "policy named: the chosen layout is applied at login", lay.returncode == 0 and back,
          f"layout save exit {lay.returncode} {lay.stderr.strip()}; editor at {e.frame(g)} want {want}")

    # named with confirmation: asked first, nothing moved
    setcfg({"confirm_before_restore": True})
    relogin()
    got = e.wait_for(asked, 15.0, 0.3)
    alone = left_alone(want)
    a = (got or [{}])[0]
    check("RS6", "policy named with 'confirm before restoring': the layout is offered at login, not applied",
          alone and a.get("reason") == "named_layout" and a.get("layout") == "rs-login", f"asked {got}; editor left alone {alone}")

    # back to how the rest of the suite expects it
    setcfg({"policy": "last_clean", "confirm_before_restore": False})
    e.stop_daemon()
    e.env["FRACTILE_UI"] = "/bin/true"
    e.start_daemon()  # same session: no login restore


def layouts_lr(e: Env, g: str) -> None:
    LM, UN = "org.fractile.Launchme", "org.fractile.Unicode"

    def wins(app):
        return [w for w in e.cli_json("list-windows") if w.get("app_id") == app]

    def close_all(*apps):
        for app in apps:
            for w in wins(app):
                try:
                    os.kill(w["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
        e.wait_for(lambda: not any(wins(a) for a in apps), 10.0, 0.3)

    def entry(eid, app, rect, launch=None, instances=1, output=None):
        en = {"id": eid, "label": app, "matcher": {"app_id": app},
              "placement": {"kind": "normalized", "rect": rect}, "instances": instances}
        if output is not None:
            en["output"] = output
        if launch is not None:
            en["launch"] = {"desktop_id": f"{app}.desktop", "policy": "if_missing", **launch}
        return en

    def save(layout):
        c = e.client()
        try:
            return c.call("layout_save", {"layout": {"schema_version": 1, **layout}})
        finally:
            c.close()

    def apply(name, mode, *extra):
        return e.run("layout", "apply", name, "--mode", mode, "--approve-all", *extra, timeout=120)

    def out_of(frame):
        for o in e.cli_json("list-outputs"):
            r = o["rect"]
            cx, cy = frame["x"] + frame["w"] // 2, frame["y"] + frame["h"] // 2
            if r["x"] <= cx < r["x"] + r["w"] and r["y"] <= cy < r["y"] + r["h"]:
                return o
        return None

    def expect(o, rect):
        wa = o["work_area"]
        return {"x": wa["x"] + round(rect["x"] * wa["w"]), "y": wa["y"] + round(rect["y"] * wa["h"]),
                "w": round(rect["w"] * wa["w"]), "h": round(rect["h"] * wa["h"])}

    close_all(LM, UN)
    c = e.client()
    c.call("set_config", {"patch": {"session": {"policy": "manual"}}})
    c.close()
    right = {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}
    left = {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0}

    # LR-03 position_existing: a missing app is not launched
    save({"name": "lr-launch", "variants": [{"entries": [entry("lm", LM, right, launch={})]}]})
    pe = apply("lr-launch", "position_existing")
    time.sleep(3)
    none_launched = not wins(LM)
    # LR-03 launch_missing: launched and placed
    lmr = apply("lr-launch", "launch_missing")
    lw = e.wait_for(lambda: wins(LM), 15.0, 0.3)
    o0 = e.cli_json("list-outputs")[0]
    placed = bool(lw) and e.wait_for(lambda: rect_eq(e.frame(lw[0]["id"]), expect(o0, right), 3), 8.0, 0.3)
    check("X32", "layout modes (LR-03): position_existing leaves a missing app closed; launch_missing launches it and places it",
          pe.returncode == 6 and none_launched and lmr.returncode == 0 and bool(placed),
          f"position_existing exit {pe.returncode}, launched {not none_launched}; launch_missing exit {lmr.returncode} {lmr.stderr.strip()[-200:]}; frame {lw and e.frame(lw[0]['id'])} want {expect(o0, right)}")

    # LR-03 exact_n: 3 instances wanted, 1 open -> 2 launched; with 3 open, none
    save({"name": "lr-three", "variants": [{"entries": [entry("lm", LM, right, launch={}, instances=3)]}]})
    ex = apply("lr-three", "exact_n")
    three = e.wait_for(lambda: (lambda l: l if len(l) == 3 else None)(wins(LM)), 30.0, 0.5)
    time.sleep(2)
    ex2 = apply("lr-three", "exact_n")
    time.sleep(4)
    n_after = len(wins(LM))
    save({"name": "lr-two", "variants": [{"entries": [entry("lm", LM, right, launch={}, instances=2)]}]})
    ex3 = apply("lr-two", "exact_n")
    time.sleep(2)
    n_kept = len(wins(LM))
    check("X33", "layout mode exact_n (LR-03): 3 instances wanted with 1 open launches 2; applying again launches none; asking for 2 with 3 open closes nothing",
          ex.returncode == 0 and bool(three) and ex2.returncode == 0 and n_after == 3 and ex3.returncode in (0, 6) and n_kept == 3,
          f"exit {ex.returncode}/{ex2.returncode}/{ex3.returncode}; windows {len(three or [])}, after re-apply {n_after}, after asking for 2: {n_kept}")
    close_all(LM, UN)

    # LR-05: an app group in order, the second after the first has a window
    # and a 2 s delay
    save({"name": "lr-stack", "variants": [{"entries": [
        entry("un", UN, right, launch={"depends_on": ["lm"], "delay_ms": 2000, "group": "stack"}),
        entry("lm", LM, left, launch={"group": "stack"})]}]})
    t0 = time.time()
    seen: dict[str, float] = {}
    proc = subprocess.Popen([str(BIN / "fractile"), "layout", "apply", "lr-stack", "--mode", "launch_missing", "--approve-all"], env=e.env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while time.time() - t0 < 30 and len(seen) < 2:
        for w in e.cli_json("list-windows"):
            if w.get("app_id") in (LM, UN) and w["app_id"] not in seen:
                seen[w["app_id"]] = time.time() - t0
        time.sleep(0.05)
    proc.wait(60)
    run = e.cli_json("restore-status")["recent_runs"][0]
    gap = seen.get(UN, 0) - seen.get(LM, 1e9)
    lmw, unw = wins(LM), wins(UN)
    ok_place = bool(lmw and unw) and rect_eq(e.frame(lmw[0]["id"]), expect(o0, left), 3) and rect_eq(e.frame(unw[0]["id"]), expect(o0, right), 3)
    check("X34", "ordered, delayed launching (LR-05): the dependent app starts only after the first has a window, at least its 2 s delay later; both placed",
          proc.returncode == 0 and LM in seen and UN in seen and gap >= 2.0 and ok_place,
          f"first window {seen.get(LM)} s, second {seen.get(UN)} s (gap {gap:.2f} s); exit {proc.returncode}; run {run.get('state')} {run.get('summary')}; placed {ok_place}")
    close_all(LM, UN)

    # LR-04 / LR-07: two monitors appear (RandR monitors on the Xephyr
    # screen); a layout's per-monitor variant and a profile switch follow
    xr = lambda *a: subprocess.run(["xrandr", *a], env=e.env, capture_output=True, text=True)
    ga = right
    save({"name": "lr-topo", "variants": [
        {"when_output_count": 1, "entries": [entry("g", "org.fractile.G", left)]},
        {"when_output_count": 2, "entries": [entry("g", "org.fractile.G", ga, output={"kind": "index", "value": 1})]}]})
    gw = next((w for w in e.cli_json("list-windows") if w["id"] == g), None)
    gapp = gw and gw.get("app_id")
    if gapp != "org.fractile.G":
        # matchers use the editor's real app id
        save({"name": "lr-topo", "variants": [
            {"when_output_count": 1, "entries": [entry("g", gapp, left)]},
            {"when_output_count": 2, "entries": [entry("g", gapp, ga, output={"kind": "index", "value": 1})]}]})
    one = apply("lr-topo", "position_existing")
    f1 = e.frame(g)
    ok1 = one.returncode == 0 and rect_eq(f1, expect(e.cli_json("list-outputs")[0], left), 3)
    # profile for the two-monitor setup: its login layout is lr-topo
    r1 = xr("--setmonitor", "fr-a", "800/200x1200/300+0+0", "default")
    r2 = xr("--setmonitor", "fr-b", "800/200x1200/300+800+0", "none")
    outs = e.wait_for(lambda: (lambda o: o if len(o) == 2 else None)(e.cli_json("list-outputs")), 10.0, 0.3) or []
    sig = "+".join(sorted(f"{o['stable_id']}@{o['rect']['w']}x{o['rect']['h']}" for o in outs))
    two = apply("lr-topo", "position_existing")
    f2 = e.frame(g)
    ob = sorted(outs, key=lambda o: o["rect"]["x"])[1] if len(outs) == 2 else None
    ok2 = two.returncode == 0 and ob is not None and rect_eq(f2, expect(ob, ga), 3)
    check("X35", "per-monitor variants (LR-04): with one monitor the one-monitor variant applies; with two, the two-monitor variant puts the window on the second",
          ok1 and ok2, f"one monitor {f1} (exit {one.returncode}); setmonitor {r1.returncode}/{r2.returncode}; outputs {[o['stable_id'] for o in outs]}; two monitors {f2} want {ob and expect(ob, ga)} (exit {two.returncode})")
    # back to one monitor, move the window, then let the profile switch do it
    xr("--delmonitor", "fr-b")
    xr("--delmonitor", "fr-a")
    e.wait_for(lambda: len(e.cli_json("list-outputs")) == 1, 10.0, 0.3)
    e.cli_json("snap", "center", "--window", g)
    c = e.client()
    c.call("set_config", {"patch": {"auto_switch_profiles": True, "active_profile": None,
                                    "profiles": [{"name": "docked", "topology": sig, "login_layout": "lr-topo"}]}})
    c.close()
    xr("--setmonitor", "fr-a", "800/200x1200/300+0+0", "default")
    xr("--setmonitor", "fr-b", "800/200x1200/300+800+0", "none")
    moved = e.wait_for(lambda: ob is not None and rect_eq(e.frame(g), expect(ob, ga), 3), 15.0, 0.3)
    active = e.cli_json("config", "get", "active_profile")
    logl = [l for l in (e.base / "daemon.out").read_text().splitlines() if "matched profile" in l][-1:]
    check("X36", "profile auto-switch (LR-07): when the docked monitor setup appears, its profile becomes active and its layout is applied",
          bool(moved) and active == "docked", f"active profile {active!r}; window {e.frame(g)}; {logl}")
    xr("--delmonitor", "fr-b")
    xr("--delmonitor", "fr-a")
    e.wait_for(lambda: len(e.cli_json("list-outputs")) == 1, 10.0, 0.3)
    c = e.client()
    c.call("set_config", {"patch": {"auto_switch_profiles": False, "active_profile": None, "profiles": []}})
    c.close()


UNI_TITLE = "Résumé 履歴書 Ünïcode ✓ 🎉"


def t20(e: Env) -> None:
    def win(app_id, pred=lambda w: True, all_=False, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            # a GTK dialog carries the app's WM_CLASS but no application id
            ws = [w for w in e.cli_json("list-windows", *(["--all"] if all_ else [])) if app_id in (w.get("app_id"), w.get("wm_class")) and pred(w)]
            if ws:
                return ws
            time.sleep(0.2)
        return []

    # no restore of the earlier checks may still be running
    e.wait_for(lambda: e.cli_json("restore-status").get("running") is None, 30.0, 0.3)

    # X29: unicode app name and title survive listing, snapshot, restore; redacted output hides them
    e.window("gtk4", "org.fractile.Unicode", UNI_TITLE)
    u = (win("org.fractile.Unicode") or [None])[0]
    if not check("X29a", "a window with a unicode title is listed with its exact title", bool(u) and u.get("title") == UNI_TITLE, u and u.get("title")):
        return
    e.cli_json("snap", "left-third", "--window", u["id"])
    want = e.frame(u["id"])
    e.cli_json("save", "--name", "unicode")
    snap = e.cli_json("snapshot", "export", "unicode")
    rec = next((r for r in snap["windows"] if r.get("app_id") == "org.fractile.Unicode"), {})
    e.cli_json("snap", "center", "--window", u["id"])
    e.cli_json("restore", "--snapshot", "unicode", "--no-launch")
    back = e.wait_for(lambda: rect_eq(e.frame(u["id"]), want, 2), 8.0, 0.2)
    red = e.run("snapshot", "export", "unicode", "--redacted").stdout
    logs = "\n".join(e.cli_json("logs", "--redacted", "--lines", "2000"))
    check("X29", "unicode app name and title: identified by desktop id, recorded, restored exactly; redacted export and logs omit the title",
          rec.get("desktop_file_id") == "org.fractile.Unicode.desktop" and rec.get("title_hint") == UNI_TITLE and bool(back) and "履歴書" not in red and "履歴書" not in logs,
          f"desktop id {rec.get('desktop_file_id')}, title hint ok {rec.get('title_hint') == UNI_TITLE}, restored {bool(back)}, title in redacted export {'履歴書' in red}, in redacted logs {'履歴書' in logs}")

    # X30: two windows of one app; one is retitled after the snapshot; each still goes back to its own place
    tw = e.window("gtk4", "org.fractile.Twins", "Doc", "--windows", "2", "--retitle", "Doc 1 (edited)")
    ws = win("org.fractile.Twins", timeout=15.0)
    ws = e.wait_for(lambda: (lambda l: l if len(l) == 2 else None)(win("org.fractile.Twins", timeout=1.0)), 10.0, 0.2) or ws
    d1 = next((w for w in ws if w.get("title") == "Doc 1"), None)
    d2 = next((w for w in ws if w.get("title") == "Doc 2"), None)
    if not check("X30a", "two windows of the same app are listed separately", bool(d1 and d2), str([w.get("title") for w in ws])):
        return
    e.cli_json("snap", "left-half", "--window", d1["id"])
    e.cli_json("snap", "right-half", "--window", d2["id"])
    w1, w2 = e.frame(d1["id"]), e.frame(d2["id"])
    e.cli_json("save", "--name", "twins")
    os.kill(tw.pid, signal.SIGUSR1)
    renamed = win("org.fractile.Twins", lambda w: w.get("title") == "Doc 1 (edited)", timeout=5.0)
    e.cli_json("snap", "right-half", "--window", d1["id"])
    e.cli_json("snap", "left-half", "--window", d2["id"])
    rep = e.cli_json("restore", "--snapshot", "twins", "--no-launch")
    ok = e.wait_for(lambda: rect_eq(e.frame(d1["id"]), w1, 2) and rect_eq(e.frame(d2["id"]), w2, 2), 8.0, 0.2)
    n = len(win("org.fractile.Twins", timeout=1.0))
    check("X30", "a window retitled after the snapshot keeps its identity: both same-app windows return to their own places, nothing duplicated",
          bool(renamed) and bool(ok) and n == 2, f"retitled {bool(renamed)}; Doc 1 at {e.frame(d1['id'])} want {w1}; Doc 2 at {e.frame(d2['id'])} want {w2}; windows {n}; report {json.dumps(rep)[:200]}")

    # X31: a transient dialog is never a placement target and never recorded
    e.window("gtk4", "org.fractile.Dlg", "Main", "--dialog")
    allw = win("org.fractile.Dlg", lambda w: True, all_=True)
    allw = e.wait_for(lambda: (lambda l: l if len(l) >= 2 else None)(win("org.fractile.Dlg", all_=True, timeout=1.0)), 10.0, 0.2) or allw
    dlg = next((w for w in allw if w.get("title") == "Main dialog"), None)
    main_ = next((w for w in allw if w.get("title") == "Main"), None)
    placeable = [w.get("title") for w in win("org.fractile.Dlg", timeout=3.0)]
    before = e.frame(dlg["id"]) if dlg else None
    e.cli_json("save", "--name", "dialog")
    recs = [r.get("title_hint") for r in e.cli_json("snapshot", "export", "dialog")["windows"] if "org.fractile.Dlg" in (r.get("app_id"), r.get("wm_class"))]
    e.cli_json("restore", "--snapshot", "dialog", "--no-launch")
    after = e.frame(dlg["id"]) if dlg else None
    check("X31", "a transient dialog is seen (list-windows --all) but never offered for placement, recorded, or moved by a restore",
          bool(dlg and main_) and placeable == ["Main"] and recs == ["Main"] and before == after,
          f"all {[w.get('title') for w in allw]}; placeable {placeable}; recorded {recs}; dialog frame {before} -> {after}")


if __name__ == "__main__":
    sys.exit(main())
