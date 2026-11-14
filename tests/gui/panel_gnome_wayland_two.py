#!/usr/bin/env python3
"""Two-monitor panel checks on GNOME Shell Wayland (DV-12, DV-13): a headless
gnome-shell --wayland with two virtual monitors side by side, the Fractile
extension and the real PyQt6 UI on Wayland. The X11 counterpart is
tests/gui/gui_two_monitors.py (G22, G23).

Input comes from the test harness through Mutter's RemoteDesktop virtual
keyboard and pointer (tests/tools/wlkeys.py); Fractile injects nothing.

Usage: tests/gui/panel_gnome_wayland_two.py [--keep] [--evidence FILE]
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
sys.path.insert(0, str(ROOT / "tests" / "gui"))
from e2e_gnome_wayland import EXT_UUID, Env, check, rect_eq, results  # noqa: E402
from panel_gnome_wayland import UNSAFE, Input, pointer, preview  # noqa: E402


def inside(r, o) -> bool:
    cx, cy = r["x"] + r["w"] // 2, r["y"] + r["h"] // 2
    return o["x"] <= cx < o["x"] + o["w"] and o["y"] <= cy < o["y"] + o["h"]


def run_all(e: Env) -> None:
    geom = e.base / "panel-geometry.json"
    e.env["FRACTILE_UI_TEST_GEOMETRY"] = str(geom)
    e.start_daemon()
    e.wait_for(lambda: e.status()["window_control"], 10.0)
    e.run("config", "set", "general.panel_shortcut", '"Ctrl+Alt+G"')
    e.run("config", "set", "general.first_run_complete", "true")
    e.run("config", "set", "general.tray_icon", "false")
    outs = e.cli_json("list-outputs")
    if not check("PM00", "two monitors are seen", len(outs) == 2, f"{[(o['stable_id'], o['rect']) for o in outs]}"):
        return
    by_out = {o["stable_id"]: o["rect"] for o in outs}
    e.window("gtk4", "org.fractile.Editor", "Editor window")
    ed = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Editor")
    if not check("PM01", "test window is up and the GNOME integration is connected (Wayland)", bool(ed)):
        return
    wid = ed["id"]
    e.cli_json("snap", "center", "--window", wid)
    home = next(o for o in outs if inside(e.frame(wid), o["rect"]))
    inp = Input(e.env, lambda: pointer(e))

    def panels():
        return [w for w in e.windows(True) if w["title"].startswith("Fractile panel") and not w["state"]["minimized"]]

    geom.unlink(missing_ok=True)
    e.activate(wid)
    time.sleep(0.3)
    inp.keys("ctrl+alt+g")
    grids = e.wait_for(lambda: (lambda d: d if d and len(d) == 2 else None)(json.loads(geom.read_text()) if geom.exists() and geom.stat().st_size else None), 10.0, 0.2) or []
    e.wait_for(lambda: len(panels()) == 2 or None, 5.0, 0.2)
    # the extension places each new panel after it is mapped: wait for the frames to settle
    last, since, end = None, time.time(), time.time() + 6.0
    while time.time() < end and time.time() - since < 0.8:
        cur = sorted((p["frame"]["x"], p["frame"]["y"]) for p in panels())
        if cur != last:
            last, since = cur, time.time()
        time.sleep(0.1)
    pw = panels()
    # each grid belongs to the panel window on its output
    matched = []
    for g in grids:
        o = by_out.get(g["output"])
        w = next((p for p in pw if o and inside(p["frame"], o)), None)
        if w:
            matched.append(dict(g, x=w["frame"]["x"] + g["ox"], y=w["frame"]["y"] + g["oy"]))
    check("PM02", "the shortcut opens one panel per monitor, each on its own monitor (Wayland)",
          len(pw) == 2 and len(matched) == 2 and len({m["output"] for m in matched}) == 2,
          f"panels {[p['frame'] for p in pw]}; grids {[(g['output'], g.get('ox'), g.get('oy')) for g in grids]}")
    other = next((m for m in matched if m["output"] != home["stable_id"]), None)
    if not other:
        check("PM03", "dragging on the other monitor's panel places the window on that monitor exactly", False, "no panel for the other monitor")
        inp.close()
        return

    def cell(g, col, row):
        cw, ch = g["w"] / g["cols"], g["h"] / g["rows"]
        return int(g["x"] + (col + 0.5) * cw), int(g["y"] + (row + 0.5) * ch)

    ax, ay = cell(other, 1, 1)
    bx, by = cell(other, 3, 4)
    inp.move_to(ax, ay)
    time.sleep(0.1)
    inp.send("button down")
    time.sleep(0.15)
    inp.move_to((ax + bx) // 2, (ay + by) // 2)
    inp.move_to(bx, by)
    time.sleep(0.4)
    pv = preview(e)
    inp.send("button up")
    c = e.client()
    try:
        want = c.call("compute_rect", {"output": other["output"], "grid": {"cols": other["cols"], "rows": other["rows"]},
                                       "selection": {"col0": 1, "row0": 1, "col1": 3, "row1": 4}})
    finally:
        c.close()
    got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want, 2) else None)(e.frame(wid)), 4.0) or e.frame(wid)
    check("PM03", "dragging on the other monitor's panel places the window on that monitor exactly (Wayland)",
          rect_eq(got, want, 2) and inside(got, by_out[other["output"]]), f"want {want} got {got} (from {home['stable_id']} to {other['output']})")
    check("PM04", "during that drag the preview is on the other monitor at the exact target",
          isinstance(pv, dict) and rect_eq(pv, want, 2), f"preview {pv} want {want}")
    check("PM05", "both panels close after the placement", bool(e.wait_for(lambda: not panels() or None, 3.0)), f"{len(panels())} open")
    inp.close()
    log = e.ui_log.read_text(errors="replace") if e.ui_log.exists() else ""
    check("PM06", "the UI logged no Python errors", "Traceback" not in log, log[-300:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "panel-gnome-wayland-two-monitors.json"))
    a = ap.parse_args()
    e = Env(monitors="1600x1200,1280x1024", extra_exts=[UNSAFE])
    try:
        run_all(e)
    except Exception as ex:  # noqa: BLE001
        check("PMXX", "suite crashed", False, repr(ex))
    finally:
        e.cleanup(a.keep)
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "panel-gnome-wayland-two-monitors", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"headless {gs} --wayland --no-x11, two virtual monitors (1600x1200, 1280x1024), Fractile extension {EXT_UUID}, "
                         "PyQt6 UI on Wayland; input from Mutter's RemoteDesktop virtual keyboard and pointer (test harness)",
          "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
