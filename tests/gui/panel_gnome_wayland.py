#!/usr/bin/env python3
"""Panel checks on GNOME Shell Wayland (DV-04, DV-05, DV-07, DV-09, DV-11,
DV-15, DV-21, DV-24): the same interactions as the X11 GUI suite
(tests/gui/gui_x11.py G03/G14, G15, G08, G07, G09, G16, G19), against a
headless gnome-shell --wayland with the Fractile extension and the real PyQt6
UI on Wayland.

Input comes from the test harness through Mutter's RemoteDesktop virtual
keyboard and pointer (tests/tools/wlkeys.py); Fractile injects nothing. A
test-only extension (tests/tools/unsafe-mode@fractile.test) lets the harness
read the pointer position and the preview the Fractile extension draws.

Usage: tests/gui/panel_gnome_wayland.py [--keep] [--evidence FILE]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "integration"))
from e2e_gnome_wayland import EXT_UUID, Env, check, expected, rect_eq, results  # noqa: E402

UNSAFE = ROOT / "tests" / "tools" / "unsafe-mode@fractile.test"
WLKEYS = ROOT / "tests" / "tools" / "wlkeys.py"


class Input:
    """One virtual keyboard and pointer for the whole run."""

    def __init__(self, env: dict, where):
        self.p = subprocess.Popen([sys.executable, str(WLKEYS), "--stdin"], env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        if self.p.stdout.readline().strip() != "ready":
            raise RuntimeError("wlkeys did not start")
        self.where = where  # reads the real pointer position

    def send(self, *lines: str) -> None:
        for line in lines:
            self.p.stdin.write(line + "\n")
            self.p.stdin.flush()
            if self.p.stdout.readline().strip() != "ok":
                raise RuntimeError(f"wlkeys failed on {line!r}")

    def keys(self, *combos: str) -> None:
        self.send(*combos)

    def move_to(self, x: int, y: int) -> None:
        # Start from where the pointer really is. (Sending it to a corner
        # first would hit GNOME's hot corner and open the overview.)
        self.x, self.y = self.where()
        # small steps, so motion compression or edge resistance cannot eat a big jump
        while (self.x, self.y) != (x, y):
            dx = max(-200, min(200, x - self.x))
            dy = max(-200, min(200, y - self.y))
            self.send(f"move {dx} {dy}")
            self.x += dx
            self.y += dy

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(5)
        except Exception:  # noqa: BLE001
            self.p.kill()


def shell_eval(e: Env, js: str):
    """org.gnome.Shell.Eval on the test shell's bus (allowed by the unsafe-mode
    test extension). Returns the decoded JSON value, or None."""
    from gi.repository import Gio, GLib
    bus = Gio.DBusConnection.new_for_address_sync(e.env["DBUS_SESSION_BUS_ADDRESS"],
                                                  Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
    try:
        ok, val = bus.call_sync("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell", "Eval", GLib.Variant("(s)", (js,)),
                                GLib.VariantType("(bs)"), 0, 10000, None).unpack()
    except GLib.Error:
        return None
    finally:
        bus.close_sync(None)
    if not ok:
        return None
    v = json.loads(val) if val else None
    while isinstance(v, str):  # Eval JSON-encodes the result, which is itself JSON text
        try:
            v = json.loads(v)
        except ValueError:
            break
    return v


def pointer(e: Env):
    v = shell_eval(e, "JSON.stringify(global.get_pointer().slice(0, 2))")
    return tuple(v) if isinstance(v, list) else None


def preview(e: Env):
    """The preview the Fractile extension draws (DV-21), or None if hidden."""
    js = ("(() => { const a = Main.extensionManager.lookup('%s')?.stateObj?._overlay; "
          "return JSON.stringify(a && a.visible ? {x: a.x, y: a.y, w: a.width, h: a.height, reactive: a.reactive} : null); })()") % EXT_UUID
    return shell_eval(e, js)


def run_all(e: Env) -> None:
    geom = e.base / "panel-geometry.json"
    e.env["FRACTILE_UI_TEST_GEOMETRY"] = str(geom)  # the UI inherits the service's environment
    e.start_daemon()
    e.wait_for(lambda: e.status()["window_control"], 10.0)
    e.run("config", "set", "general.panel_shortcut", '"Ctrl+Alt+G"')
    e.run("config", "set", "general.first_run_complete", "true")
    e.run("config", "set", "general.tray_icon", "false")
    e.window("gtk4", "org.fractile.Editor", "Editor window")
    ed = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Editor")
    st = e.status()
    if not check("PW00", "test window is up and the GNOME integration is connected (Wayland)",
                 bool(ed) and st["adapter"] == "gnome" and st["session_type"] == "wayland", f"adapter={st['adapter']} session={st['session_type']}"):
        return
    wid = ed["id"]
    inp = Input(e.env, lambda: pointer(e))

    def panel_window():
        return next((w for w in e.windows(True) if w["title"].startswith("Fractile panel") and not w["state"]["minimized"]), None)

    def open_panel():
        geom.unlink(missing_ok=True)
        e.activate(wid)
        time.sleep(0.3)
        inp.keys("ctrl+alt+g")
        g = e.wait_for(lambda: (lambda d: d or None)(json.loads(geom.read_text()) if geom.exists() and geom.stat().st_size else None), 10.0, 0.2)
        pw = e.wait_for(panel_window, 5.0, 0.2)
        if not g or not pw:
            return None
        # On the first open the extension moves the new window into place
        # after it is mapped: wait until its frame stops changing.
        last, since = pw["frame"], time.time()
        end = time.time() + 5.0
        while time.time() < end and time.time() - since < 0.6:
            time.sleep(0.1)
            cur = panel_window()
            if cur is None:
                return None
            if cur["frame"] != last:
                last, since = cur["frame"], time.time()
        pw = dict(pw, frame=last)
        g = dict(g[0])
        # screen position of the grid = panel window frame + grid offset in it
        g["x"], g["y"] = pw["frame"]["x"] + g["ox"], pw["frame"]["y"] + g["oy"]
        time.sleep(0.3)
        return g

    def cell(g, col, row):
        cw, ch = g["w"] / g["cols"], g["h"] / g["rows"]
        return int(g["x"] + (col + 0.5) * cw), int(g["y"] + (row + 0.5) * ch)

    def closed():
        return e.wait_for(lambda: panel_window() is None, 3.0)

    def close_if_open():
        if panel_window():
            inp.keys("escape")
            closed()

    def drag(g, a, b, hold=None):
        ax, ay = cell(g, *a)
        bx, by = cell(g, *b)
        inp.move_to(ax, ay)
        time.sleep(0.1)
        if hold:
            inp.send(f"keydown {hold}")
            time.sleep(0.3)
        inp.send("button down")
        time.sleep(0.15)
        inp.move_to((ax + bx) // 2, (ay + by) // 2)
        inp.move_to(bx, by)
        time.sleep(0.4)
        mid = (pointer(e), preview(e), e.frame(wid))
        inp.send("button up")
        time.sleep(0.1)
        if hold:
            inp.send(f"keyup {hold}")
        return mid

    def placed(want):
        return e.wait_for(lambda: (lambda f: f if rect_eq(f, want, 2) else None)(e.frame(wid)), 4.0) or e.frame(wid)

    # PW01 + PW02: drag across the grid (DV-04) with the preview (DV-21)
    e.cli_json("snap", "center", "--window", wid)
    g = open_panel()
    if not check("PW01a", "the panel opens on the shortcut and reports its grid", bool(g), "" if g else "no panel or geometry"):
        return
    want = expected(e, g["cols"], g["rows"], 0, 0, 2, g["rows"] - 1)
    ptr, pv, before = drag(g, (0, 0), (2, g["rows"] - 1))
    bx, by = cell(g, 2, g["rows"] - 1)
    got = placed(want)
    check("PW01", "dragging across the grid places the window exactly (Wayland)", rect_eq(got, want, 2),
          f"want {want} got {got}; pointer at drag end {ptr} (aimed at {(bx, by)})")
    check("PW02", "during the drag the extension draws a non-interactive preview of the exact target, and the window has not moved yet",
          bool(pv) and isinstance(pv, dict) and rect_eq(pv, want, 2) and not pv.get("reactive") and not rect_eq(before, want, 2),
          f"preview {pv} want {want}; window then {before}")
    check("PW02b", "the preview is gone after the drop", e.wait_for(lambda: preview(e) is None, 3.0), f"{preview(e)}")
    closed()

    # PW03: Esc cancels with no side effects and returns focus (DV-09)
    before = e.frame(wid)
    open_panel()
    inp.keys("right", "escape")
    gone = closed()
    time.sleep(0.5)
    check("PW03", "Esc closes the panel without moving the window and focus returns to it",
          bool(gone) and rect_eq(e.frame(wid), before, 0) and e.wait_for(lambda: e.active() == wid, 3.0), f"active {e.active()}; {before} -> {e.frame(wid)}")

    # PW04: Alt+= / Alt+Shift+= change columns / rows, and the size is kept (DV-07, DV-24)
    g1 = open_panel()
    inp.keys("alt+equal")
    time.sleep(0.3)
    inp.keys("alt+shift+equal")
    time.sleep(0.3)
    inp.keys("ctrl+alt+g")  # toggles closed
    closed()
    g2 = open_panel()
    ok = bool(g1 and g2) and (g2["cols"], g2["rows"]) == (g1["cols"] + 1, g1["rows"] + 1)
    check("PW04", "Alt+= / Alt+Shift+= add a column / row, and the new size is kept the next time the panel opens",
          ok, f"{g1 and (g1['cols'], g1['rows'])} -> {g2 and (g2['cols'], g2['rows'])}")
    inp.keys("alt+minus")
    time.sleep(0.2)
    inp.keys("alt+shift+minus")
    time.sleep(0.2)
    close_if_open()

    # PW05: Shift+drag keeps the panel open (DV-11)
    g = open_panel()
    if g:
        want = expected(e, g["cols"], g["rows"], g["cols"] - 2, 0, g["cols"] - 1, 1)
        drag(g, (g["cols"] - 2, 0), (g["cols"] - 1, 1), hold="shift")
        got = placed(want)
        still = panel_window() is not None
        check("PW05", "Shift+drag places the window and keeps the panel open", rect_eq(got, want, 2) and still, f"want {want} got {got} panel_open={still}")
    else:
        check("PW05", "Shift+drag places the window and keeps the panel open", False, "panel did not open")
    close_if_open()

    # PW06: holding Alt subdivides 10x10 into 20x20 (DV-05)
    e.run("config", "set", "appearance.default_grid", '{"cols": 10, "rows": 10}')
    time.sleep(0.5)
    g = open_panel()
    if g and (g["cols"], g["rows"]) == (10, 10):
        sub = dict(g, cols=20, rows=20)
        drag(sub, (1, 1), (12, 14), hold="alt")
        want = expected(e, 20, 20, 1, 1, 12, 14)
        got = placed(want)
        check("PW06", "holding Alt subdivides 10x10 to 20x20 and a half-cell selection lands exactly", rect_eq(got, want, 2), f"want {want} got {got}")
    else:
        check("PW06", "holding Alt subdivides 10x10 to 20x20 and a half-cell selection lands exactly", False, f"panel grid {g and (g['cols'], g['rows'])}")
    close_if_open()
    e.run("config", "set", "appearance.default_grid", '{"cols": 6, "rows": 6}')
    time.sleep(0.5)

    # PW07: a panel-local shortcut acts only while the panel is open (DV-15)
    e.cli_json("snap", "center", "--window", wid)
    open_panel()
    inp.keys("ctrl+left")
    want = expected(e, 2, 1, 0, 0, 0, 0)
    got = placed(want)
    in_panel = rect_eq(got, want, 2)
    was_closed = closed()
    e.cli_json("snap", "center", "--window", wid)
    centered = e.frame(wid)
    e.activate(wid)
    time.sleep(0.3)
    inp.keys("ctrl+left")
    time.sleep(0.8)
    check("PW07", "panel-local shortcut (Ctrl+Left) snaps while the panel is open and does nothing once it is closed",
          in_panel and bool(was_closed) and rect_eq(e.frame(wid), centered, 0), f"in panel {got}; after close {e.frame(wid)} (centered {centered})")

    # PW08: reset the grid each time the panel opens (DV-24)
    e.run("config", "set", "appearance.reset_grid_on_open", "true")
    time.sleep(0.5)
    g1 = open_panel()
    inp.keys("alt+equal")
    time.sleep(0.4)
    close_if_open()
    g2 = open_panel()
    check("PW08", "with reset on open, a grid changed with Alt+= is back to the default 6x6 next time",
          bool(g1 and g2) and (g1["cols"], g2["cols"], g2["rows"]) == (6, 6, 6), f"{g1 and (g1['cols'], g1['rows'])} -> {g2 and (g2['cols'], g2['rows'])}")
    close_if_open()
    e.run("config", "set", "appearance.reset_grid_on_open", "false")
    inp.close()

    log = e.ui_log.read_text(errors="replace") if e.ui_log.exists() else ""
    check("PW09", "the UI logged no Python errors", "Traceback" not in log, log[-300:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "panel-gnome-wayland.json"))
    a = ap.parse_args()
    e = Env(extra_exts=[UNSAFE])
    try:
        run_all(e)
    except Exception as ex:  # noqa: BLE001
        check("PWXX", "suite crashed", False, repr(ex))
    finally:
        e.cleanup(a.keep)
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "panel-gnome-wayland", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"headless {gs} --wayland --no-x11, Fractile extension {EXT_UUID}, PyQt6 UI on Wayland; "
                         "input from Mutter's RemoteDesktop virtual keyboard and pointer (test harness)",
          "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
