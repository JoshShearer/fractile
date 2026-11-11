#!/usr/bin/env python3
"""GUI end-to-end tests: the real PyQt6 UI on a real window manager.

Starts an isolated nested GNOME Shell (X11), an isolated fractiled whose
FRACTILE_UI points at the real fractile-ui, real GTK4/Qt test windows, and
drives the UI the way a user does: the global hotkey, mouse drags on the grid,
keyboard navigation, the first-run wizard, the command palette and the
recovery dialog. Results are checked against the real window geometry.

Usage: tests/gui/gui_x11.py [--display 19] [--keep] [--evidence FILE]
Needs: Xephyr, gnome-shell, xdotool, xprop, python3-pyqt6.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "integration"))
from e2e_x11 import Env, check, rect_eq, results  # noqa: E402

SHOTS = ROOT / "tests" / "evidence" / "gui"


class GuiEnv(Env):
    def __init__(self, display: int):
        super().__init__(display)
        self.geom = self.base / "panel-geometry.json"
        self.ui_log = self.base / "ui.log"
        wrap = self.base / "ui-wrap.sh"
        wrap.write_text(f'#!/bin/sh\nexec python3 {ROOT / "ui" / "fractile-ui"} "$@" >>{self.ui_log} 2>&1\n')
        wrap.chmod(0o755)
        self.env["FRACTILE_UI"] = str(wrap)
        self.env["FRACTILE_UI_TEST_GEOMETRY"] = str(self.geom)
        self.env["QT_QPA_PLATFORM"] = "xcb"

    def xprop_root(self, prop: str) -> str:
        return subprocess.run(["xprop", "-root", prop], env=self.env, capture_output=True, text=True).stdout

    def active_xid(self) -> int | None:
        out = self.xprop_root("_NET_ACTIVE_WINDOW")
        try:
            return int(out.rsplit("#", 1)[1].strip().split(",")[0], 16)
        except (IndexError, ValueError):
            return None

    def stacking(self) -> list[int]:
        out = self.xprop_root("_NET_CLIENT_LIST_STACKING")
        if "#" not in out:
            return []
        return [int(x.strip(), 16) for x in out.split("#", 1)[1].split(",") if x.strip()]

    def visible_named(self, name: str) -> list[int]:
        p = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", name], env=self.env, capture_output=True, text=True)
        return [int(x) for x in p.stdout.split()]

    def wait_for(self, fn, timeout=8.0, step=0.1):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(step)
        return fn()

    def panel_geometry(self, timeout=5.0):
        def read():
            try:
                g = json.loads(self.geom.read_text())
                return g if g else None
            except (OSError, ValueError):
                return None
        return self.wait_for(read, timeout)

    def xgeom(self, xid: int) -> dict | None:
        out = subprocess.run(["xdotool", "getwindowgeometry", "--shell", str(xid)], env=self.env, capture_output=True, text=True).stdout
        kv = dict(line.split("=", 1) for line in out.split() if "=" in line)
        try:
            return {"x": int(kv["X"]), "y": int(kv["Y"]), "w": int(kv["WIDTH"]), "h": int(kv["HEIGHT"])}
        except KeyError:
            return None

    def cell_center(self, g: dict, col: int, row: int) -> tuple[int, int]:
        cw, ch = g["w"] / g["cols"], g["h"] / g["rows"]
        return int(g["x"] + (col + 0.5) * cw), int(g["y"] + (row + 0.5) * ch)

    def shot(self, name: str) -> None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        code = ("import sys\nfrom PyQt6.QtGui import QGuiApplication\n"
                "a=QGuiApplication(['x','-platform','xcb'])\ns=a.primaryScreen()\n"
                "p=s.grabWindow(0)\np=p.scaledToWidth(800)\np.save(sys.argv[1])\n")
        subprocess.run([sys.executable, "-c", code, str(SHOTS / f"{name}.png")], env=self.env, capture_output=True, timeout=20)

    def ui_pids(self) -> list[int]:
        pids = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                cmd = (d / "cmdline").read_bytes().split(b"\0")
                env = (d / "environ").read_bytes()
            except OSError:
                continue
            if any(c.endswith(b"fractile-ui") for c in cmd) and f"FRACTILE_UI_TEST_GEOMETRY={self.geom}".encode() in env:
                pids.append(int(d.name))
        return pids

    def cleanup(self):
        for pid in self.ui_pids():
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        super().cleanup()


def expected(e: GuiEnv, cols: int, rows: int, c0: int, r0: int, c1: int, r1: int) -> dict:
    c = e.client()
    try:
        return c.call("compute_rect", {"grid": {"cols": cols, "rows": rows}, "selection": {"col0": c0, "row0": r0, "col1": c1, "row1": r1}})
    finally:
        c.close()


def run_all(e: GuiEnv) -> None:
    e.start_daemon()
    e.run("config", "set", "general.panel_shortcut", '"Ctrl+Alt+G"')
    e.window("gtk4", "org.fractile.Editor", "Editor window")
    e.window("qt", "org.fractile.Viewer", "Viewer window")
    ed = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Editor")
    vw = e.wait_window(lambda w: w.get("title") == "Viewer window")
    if not check("G00", "test windows are up", bool(ed and vw)):
        return
    wid = ed["id"]
    xid = int(wid.split(":")[1], 16)

    def open_panel() -> dict | None:
        e.geom.unlink(missing_ok=True)
        e.xdo("windowactivate", "--sync", str(xid))
        time.sleep(0.3)
        e.xdo("key", "ctrl+alt+g")
        return e.panel_geometry(8.0)

    # G01: hotkey with no UI running spawns the UI and shows the panel on top
    t0 = time.time()
    g = open_panel()
    dt = time.time() - t0
    panels = e.visible_named("Fractile panel")
    st = e.cli_json("status")
    stack = e.stacking()
    on_top = bool(panels) and stack and stack[-1] in panels
    check("G01", "hotkey starts the UI and shows the panel above all windows", bool(g) and st.get("ui_attached") and on_top and e.active_xid() in panels,
          f"{dt:.1f}s incl. UI start; stack top {hex(stack[-1]) if stack else None}")
    check("G02", "first-run wizard waits while the panel is open", not e.visible_named("Welcome to Fractile"))
    e.shot("01-panel")
    if not g:
        return

    # G03: drag cells (0,0)-(2,5) of 6x6 = left half
    g = g[0]
    x0, y0 = e.cell_center(g, 0, 0)
    x1, y1 = e.cell_center(g, 2, g["rows"] - 1)
    e.xdo("mousemove", str(x0), str(y0), "mousedown", "1")
    time.sleep(0.15)
    e.xdo("mousemove", str((x0 + x1) // 2), str((y0 + y1) // 2))
    time.sleep(0.15)
    e.xdo("mousemove", str(x1), str(y1))
    time.sleep(0.5)
    e.shot("02-drag-preview")
    want = expected(e, g["cols"], g["rows"], 0, 0, 2, g["rows"] - 1)
    pv = e.visible_named("Fractile preview")
    pg = e.xgeom(pv[0]) if pv else None
    not_moved_yet = not rect_eq(e.frame(wid), want)  # the window starts elsewhere
    e.xdo("mouseup", "1")
    got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want) else None)(e.frame(wid)), 4.0) or e.frame(wid)
    check("G03", "dragging across the grid places the window exactly", rect_eq(got, want), f"want {want} got {got}")
    check("G14", "during the drag a non-interactive preview shows the exact target and the window has not moved yet",
          bool(pg) and rect_eq(pg, want, 1) and not_moved_yet, f"preview {pg} want {want}")
    hidden = e.wait_for(lambda: not e.visible_named("Fractile panel"), 3.0)
    check("G04", "panel closes after placing and focus returns to the window", hidden and e.wait_for(lambda: e.active_xid() == xid, 3.0))

    # G05: wizard appears once the panel is gone; finish it with the keyboard
    wiz = e.wait_for(lambda: e.visible_named("Welcome to Fractile"), 5.0)
    e.shot("03-wizard")
    if wiz:
        e.xdo("windowactivate", "--sync", str(wiz[0]))
        for _ in range(4):
            e.xdo("key", "alt+n")
            time.sleep(0.5)
        e.xdo("key", "alt+f")
        time.sleep(1.0)
    cfg = e.cli_json("config", "get", "general")
    check("G05", "first-run wizard appears after the panel closes and saves on Finish", bool(wiz) and cfg.get("first_run_complete") is True and cfg.get("panel_shortcut") == "Ctrl+Alt+G",
          f"first_run_complete={cfg.get('first_run_complete')} shortcut={cfg.get('panel_shortcut')}")

    # G06: keyboard selection: first arrow shows the cursor at (0,0)
    g = open_panel()
    e.xdo("key", "Right", "Right", "Right", "shift+Right", "shift+Right", "shift+Down", "shift+Down")
    time.sleep(0.4)
    e.shot("04-keyboard")
    e.xdo("key", "Return")
    want = expected(e, 6, 6, 2, 0, 4, 2)
    got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want) else None)(e.frame(wid)), 4.0) or e.frame(wid)
    check("G06", "arrow keys, Shift+arrows and Enter place the window", bool(g) and rect_eq(got, want), f"want {want} got {got}")

    # G07: Esc cancels with no side effects and returns focus
    before = e.frame(wid)
    open_panel()
    e.xdo("key", "Right", "Escape")
    time.sleep(0.8)
    check("G07", "Esc closes the panel without moving the window", rect_eq(e.frame(wid), before) and not e.visible_named("Fractile panel") and e.active_xid() == xid)

    # G08: grid size keys (Alt+= adds a column, Alt+Shift+= adds a row)
    g = open_panel()
    e.geom.unlink(missing_ok=True)
    e.xdo("key", "alt+equal")
    time.sleep(0.3)
    e.xdo("key", "alt+shift+equal")
    time.sleep(0.3)
    e.xdo("key", "ctrl+alt+g")  # toggle closes
    time.sleep(0.6)
    g2 = open_panel()
    ok = bool(g and g2) and (g2[0]["cols"], g2[0]["rows"]) == (g[0]["cols"] + 1, g[0]["rows"] + 1)
    check("G08", "Alt+= / Alt+Shift+= change columns / rows and the size is kept", ok, f"{g and (g[0]['cols'], g[0]['rows'])} -> {g2 and (g2[0]['cols'], g2[0]['rows'])}")
    e.xdo("key", "alt+minus")
    time.sleep(0.2)
    e.xdo("key", "alt+shift+minus")
    time.sleep(0.2)

    # G09: Shift+drag keeps the panel open
    e.xdo("key", "ctrl+alt+g")
    time.sleep(0.5)
    g = open_panel()
    if g:
        g = g[0]
        ax, ay = e.cell_center(g, g["cols"] - 2, 0)
        bx, by = e.cell_center(g, g["cols"] - 1, 1)
        e.xdo("mousemove", str(ax), str(ay), "keydown", "shift", "mousedown", "1")
        time.sleep(0.15)
        e.xdo("mousemove", str(bx), str(by))
        time.sleep(0.3)
        e.xdo("mouseup", "1", "keyup", "shift")
        want = expected(e, g["cols"], g["rows"], g["cols"] - 2, 0, g["cols"] - 1, 1)
        got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want) else None)(e.frame(wid)), 4.0) or e.frame(wid)
        still = bool(e.visible_named("Fractile panel"))
        check("G09", "Shift+drag places the window and keeps the panel open", rect_eq(got, want) and still, f"want {want} got {got} panel_open={still}")
        e.xdo("key", "Escape")
        time.sleep(0.5)

    # G10: command palette acts on the window that had focus
    e.xdo("windowactivate", "--sync", str(xid))
    time.sleep(0.3)
    e.run("palette")
    pal = e.wait_for(lambda: e.visible_named("Fractile command palette"), 5.0)
    time.sleep(0.4)
    e.xdo("type", "--delay", "30", "left half")
    time.sleep(0.3)
    e.shot("05-palette")
    e.xdo("key", "Return")
    want = expected(e, 2, 1, 0, 0, 0, 0)
    got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want) else None)(e.frame(wid)), 4.0) or e.frame(wid)
    check("G10", "command palette 'left half' snaps the previously focused window", bool(pal) and rect_eq(got, want), f"want {want} got {got}")

    # G11: recovery dialog restores a snapshot with progress
    saved = e.frame(wid)
    e.cli_json("save", "--name", "gui")
    e.cli_json("snap", "right-half", "--window", wid)
    c = e.client()
    try:
        c.call("show_recovery", {"snapshot_id": "gui", "reason": "gui-test"})
    finally:
        c.close()
    dlg = e.wait_for(lambda: e.visible_named("Restore windows"), 5.0)
    time.sleep(1.0)
    e.shot("06-recovery")
    if dlg:
        e.xdo("windowactivate", "--sync", str(dlg[0]))
        time.sleep(0.2)
        e.xdo("key", "Return")  # "Restore selected" is the default button
    got = e.wait_for(lambda: (lambda f: f if rect_eq(f, saved) else None)(e.frame(wid)), 8.0) or e.frame(wid)
    time.sleep(0.5)
    e.shot("07-recovery-done")
    check("G11", "recovery dialog shows the plan and restores the snapshot", bool(dlg) and rect_eq(got, saved), f"want {saved} got {got}")

    # G12: Fractile's own windows are never placement targets
    e.run("dashboard")
    e.wait_for(lambda: e.visible_named("Fractile Workspaces"), 5.0)
    own = e.visible_named("Fractile")
    listed = {int(w["id"].split(":")[1], 16) for w in e.cli_json("list-windows")}
    every = {int(w["id"].split(":")[1], 16): w for w in e.cli_json("list-windows", "--all")}
    own = [x for x in own if x in every]  # drop mutter's frame windows, which share the title
    flagged = {x for x, w in every.items() if w.get("is_own")}
    check("G12", "Fractile UI windows are flagged as own and never offered as targets", bool(own) and not (set(own) & listed) and set(own) <= flagged,
          f"{len(own)} own windows, flagged {len(flagged)}")
    e.shot("08-dashboard")

    # G15: 10x10 grid, hold Alt for 20x20 subdivision, drag half cells
    e.run("config", "set", "appearance.default_grid", '{"cols": 10, "rows": 10}')
    e.run("config", "set", "appearance.reset_grid_on_open", "true")
    e.xdo("windowactivate", "--sync", str(xid))
    time.sleep(0.3)
    g = open_panel()
    if g and (g[0]["cols"], g[0]["rows"]) == (10, 10):
        g = g[0]
        sub = dict(g, cols=20, rows=20)
        ax, ay = e.cell_center(sub, 1, 1)
        bx, by = e.cell_center(sub, 12, 14)
        e.xdo("keydown", "alt")
        time.sleep(0.3)
        e.xdo("mousemove", str(ax), str(ay), "mousedown", "1")
        time.sleep(0.15)
        e.xdo("mousemove", str(bx), str(by))
        time.sleep(0.3)
        e.shot("09-subdivided-drag")
        e.xdo("mouseup", "1")
        time.sleep(0.1)
        e.xdo("keyup", "alt")
        want = expected(e, 20, 20, 1, 1, 12, 14)
        got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want) else None)(e.frame(wid)), 4.0) or e.frame(wid)
        check("G15", "holding Alt subdivides 10x10 to 20x20 and a half-cell selection lands exactly", rect_eq(got, want), f"want {want} got {got}")
    else:
        check("G15", "holding Alt subdivides 10x10 to 20x20 and a half-cell selection lands exactly", False, f"panel grid {g and (g[0]['cols'], g[0]['rows'])}")
    e.run("config", "set", "appearance.default_grid", '{"cols": 6, "rows": 6}')
    time.sleep(0.5)

    # G16: panel-local shortcut acts only while the panel is open (DV-15)
    e.cli_json("snap", "center", "--window", wid)
    e.xdo("windowactivate", "--sync", str(xid))
    time.sleep(0.3)
    open_panel()
    e.shot("10-local-shortcut")
    e.xdo("key", "ctrl+Left")
    want = expected(e, 2, 1, 0, 0, 0, 0)
    got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want) else None)(e.frame(wid)), 4.0) or e.frame(wid)
    in_panel = rect_eq(got, want)
    closed = e.wait_for(lambda: not e.visible_named("Fractile panel"), 3.0)
    e.cli_json("snap", "center", "--window", wid)
    centered = e.frame(wid)
    e.xdo("windowactivate", "--sync", str(xid))
    time.sleep(0.3)
    e.xdo("key", "ctrl+Left")
    time.sleep(0.8)
    check("G16", "panel-local shortcut (Ctrl+Left) snaps while the panel is open and does nothing once it is closed",
          in_panel and closed and rect_eq(e.frame(wid), centered), f"in panel {got}; after close {e.frame(wid)} (centered {centered})")

    # G17: launching the app again (menu entry) shows the panel for the focused window (DV-02)
    pids = sorted(e.ui_pids())
    e.xdo("windowactivate", "--sync", str(xid))
    time.sleep(0.3)
    e.geom.unlink(missing_ok=True)
    lp = subprocess.run([sys.executable, str(ROOT / "ui" / "fractile-ui"), "--launcher"], env=e.env, capture_output=True, text=True, timeout=20)
    g = e.panel_geometry()
    shown = bool(g) and bool(e.visible_named("Fractile panel"))
    # type only once the panel has keyboard focus, as a person would
    e.wait_for(lambda: e.active_xid() in e.visible_named("Fractile panel"), 3.0, 0.05)
    e.xdo("key", "Right", "Return")
    want = expected(e, 6, 6, 0, 0, 0, 0)
    got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want) else None)(e.frame(wid)), 4.0) or e.frame(wid)
    check("G17", "relaunching from the app menu shows the panel for the focused window; no second UI stays running",
          lp.returncode == 0 and shown and rect_eq(got, want) and sorted(e.ui_pids()) == pids, f"exit {lp.returncode}; ui pids {pids} -> {sorted(e.ui_pids())}; got {got}")

    log = e.ui_log.read_text() if e.ui_log.exists() else ""
    tb = [line for line in log.splitlines() if "Traceback" in line or "Error" in line]
    check("G13", "UI logged no Python errors during the run", not tb, "; ".join(tb[:3]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", type=int, default=19)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "gui-x11.json"))
    a = ap.parse_args()
    nested = ROOT / "tests" / "integration" / "nested-gnome-x11.sh"
    st = subprocess.run(["bash", str(nested), "start", str(a.display), "1600x1200"], capture_output=True, text=True)
    if st.returncode != 0:
        print(st.stdout, st.stderr)
        return 2
    e = GuiEnv(a.display)
    try:
        run_all(e)
    except Exception as ex:  # noqa: BLE001
        check("GXX", "suite crashed", False, repr(ex))
    finally:
        e.cleanup()
        if not a.keep:
            subprocess.run(["bash", str(nested), "stop", str(a.display)])
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "gui-x11", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "environment": f"Xephyr nested {gs} (X11), PyQt6 UI", "screenshots": "tests/evidence/gui/", "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
