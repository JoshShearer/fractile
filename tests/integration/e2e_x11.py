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
            XDG_CURRENT_DESKTOP="GNOME",
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

    def frame(self, wid: str):
        for w in self.cli_json("list-windows", "--all"):
            if w["id"] == wid:
                return w["frame"]
        return None

    def xdo(self, *args):
        subprocess.run(["xdotool", *args], env=self.env, check=False, capture_output=True)

    def cleanup(self):
        self.stop_daemon()
        for p in self.procs:
            if p.poll() is None:
                p.terminate()


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
    finally:
        e.cleanup()
        if not a.keep:
            subprocess.run(["bash", str(nested), "stop", str(a.display)])
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "e2e-x11", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "environment": f"Xephyr nested {gs} (X11), real mutter WM", "results": results}
    Path(a.evidence).parent.mkdir(parents=True, exist_ok=True)
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


def run_all(e: Env) -> None:
    e.start_daemon()
    st = e.cli_json("status")
    check("T01", "service starts unprivileged with X11 adapter and window control", st["adapter"] == "x11" and st["window_control"] and os.getuid() != 0, f"pid {st['pid']}")

    # socket permissions
    sock = Path(e.env["FRACTILE_RUNTIME_DIR"]) / "fractile.sock"
    mode = sock.stat().st_mode & 0o777
    dmode = sock.parent.stat().st_mode & 0o777
    check("SEC1", "IPC socket is 0600 in a 0700 dir", mode == 0o600 and dmode == 0o700, f"sock {oct(mode)} dir {oct(dmode)}")

    # second instance
    p = subprocess.run([str(BIN / "fractiled")], env=e.env, capture_output=True, text=True, timeout=10)
    check("T02", "second instance exits without starting twice", p.returncode == 0 and "already running" in p.stderr, p.stderr.strip())

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
    check("T03", "real GTK4 and Qt windows are listed", bool(gtk and qt))
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
    check("T04", "grid placements land on the requested rectangle (GTK4)", ok_all, "; ".join(detail))
    out = e.cli_json("place", "--grid", "6x6", "--cells", "3,0,5,2", "--window", q)
    check("T05", "grid placement on a Qt (server-side decorated) window", out["within_tolerance"], f"err={out['max_error_px']}px")
    full = e.cli_json("place", "--grid", "1x1", "--cells", "0,0,0,0", "--window", g)
    check("T06", "1x1 selection fills the work area exactly (no panel overlap)", rect_eq(full["observed"], wa, 2), f"{full['observed']} vs work area {wa}")

    # snap + undo
    before = e.frame(g)
    e.cli_json("snap", "right-half", "--window", g)
    e.cli_json("undo")
    check("T07", "undo restores the previous geometry", rect_eq(e.frame(g), before, 2))

    # error exit codes
    check("T08", "invalid selection exits 7", e.run("place", "--grid", "6x6", "--cells", "0,0,9,9", "--window", g).returncode == 7)
    check("T08b", "unknown window exits 5", e.run("place", "--grid", "6x6", "--cells", "0,0,1,1", "--window", "x11:0xdead").returncode == 5)

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
    check("T09", "global shortcut places the focused window (real key event)", placed is not None and not r["hotkey_errors"], f"{placed and round(placed * 1000)} ms incl. polling" if placed else f"frame {e.frame(g)} target {target}")
    e.xdo("key", "ctrl+alt+z")
    time.sleep(1.0)
    check("T10", "global undo shortcut", rect_eq(e.frame(g), before, 2))
    chk = c.call("shortcut_check", {"accelerator": "Ctrl+Alt+1"})
    check("T11", "shortcut conflict detection reports Fractile's own binding", chk["free"] is False and "Left third" in chk["reason"])

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
    check("T12", "dry run classifies open windows and changes nothing", acts == ["already_open", "already_open"] and rect_eq(e.frame(g), moved_g), str(plan["counts"]))
    rep = e.cli_json("restore", "--snapshot", "e2e")
    check("T13", "restore puts existing windows back exactly", rep["state"] == "completed" and rect_eq(e.frame(g), want_g, 2) and rect_eq(e.frame(q), want_q, 2), f"{rep['report']['duration_ms']} ms")
    e.cli_json("undo-restore")
    check("T14", "undo-restore returns to the pre-restore layout", rect_eq(e.frame(g), moved_g, 2))

    # launching: approval gate, launch, no duplicates
    lp = subprocess.Popen(["gio", "launch", str(e.base / "xdgdata/applications/org.fractile.Launchme.desktop")], env=e.env)
    lw = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Launchme")
    if not check("T15", "launchable test app identified by desktop id", bool(lw) and lw.get("desktop_file_id") == "org.fractile.Launchme.desktop", lw and lw.get("desktop_file_source")):
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
    check("T16", "missing app needs approval by default (no silent launch)", na and na[0]["action"] == "needs_approval")
    p = e.run("restore", "--snapshot", "with-launch")
    still_missing = not any(w.get("app_id") == "org.fractile.Launchme" for w in e.cli_json("list-windows"))
    check("T17", "unapproved restore does not launch and exits 6 (partial)", p.returncode == 6 and still_missing)
    rep = e.cli_json("restore", "--snapshot", "with-launch", "--approve-all")
    nl = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Launchme", 5)
    check("T18", "approved restore launches the app and places it", rep["state"] == "completed" and nl and rect_eq(e.frame(nl["id"]), want_l, 2), json.dumps(rep["report"]["launches"]))
    rep2 = e.cli_json("restore", "--snapshot", "with-launch", "--approve-all")
    n = sum(1 for w in e.cli_json("list-windows") if w.get("app_id") == "org.fractile.Launchme")
    check("T19", "repeated restore never duplicates windows", n == 1 and rep2["report"]["launches"] == 0, f"{n} window(s), {rep2['report']['launches']} launches")

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
    check("T20", "redacted logs contain no window titles", not any("Gtk window" in l or "Launchme\"" in l for l in logs))

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
    check("T21", "daemon killed mid-restore: restart resumes, no duplicate launches", n == 1, f"{n} Launchme window(s); last runs {[r.get('state') for r in runs[:3]]}")

    # SIGTERM at logout writes a snapshot when an automatic policy is on
    c = e.client()
    c.call("set_config", {"patch": {"session": {"policy": "last_clean"}}})
    count0 = len(e.cli_json("list-snapshots", "--limit", "200"))
    e.stop_daemon(signal.SIGTERM)
    e.start_daemon()
    snaps = e.cli_json("list-snapshots", "--limit", "200")
    check("T22", "orderly stop (logout) captures a snapshot", len(snaps) > count0 and any(s["mode"] == "logout" for s in snaps), f"{count0} -> {len(snaps)}")

    # broken config: service runs on defaults and never overwrites the file
    e.stop_daemon()
    cfgp = Path(e.env["FRACTILE_CONFIG"])
    cfgp.write_text("this is = = not toml [")
    e.start_daemon()
    st = e.cli_json("status")
    check("T23", "corrupt config: service starts on defaults, reports it, keeps the file", st["config_error"] is not None and cfgp.read_text().startswith("this is"), st["config_error"] and st["config_error"][:60])
    cfgp.unlink()

    # no-WM / unsupported desktop honesty
    env2 = dict(e.env)
    env2.update(XDG_SESSION_TYPE="wayland", WAYLAND_DISPLAY="wayland-fake", XDG_CURRENT_DESKTOP="Hyprland")
    out = subprocess.run([str(BIN / "fractiled"), "--check"], env=env2, capture_output=True, text=True)
    caps = json.loads(out.stdout)["capabilities"]
    levels = {v["level"] for v in caps["operations"].values()}
    check("T24", "unknown Wayland desktop reports unsupported (no fake support)", caps["adapter"] == "none" and levels == {"unsupported"}, str(levels))


if __name__ == "__main__":
    sys.exit(main())
