#!/usr/bin/env python3
"""GUI check on two monitors (DV-13): each monitor gets its own panel, on
that monitor, and dragging on the second monitor's panel moves the window
there, exactly. Real PyQt6 UI, real X mouse events.

Usage: tests/gui/gui_two_monitors.py [--display 43] [--evidence FILE]
Containers: FRACTILE_TEST_XSERVER=xvfb splits one Xvfb screen into two RandR
monitors (see tests/integration/nested-gnome-x11.sh).
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
sys.path.insert(0, str(ROOT / "tests" / "gui"))
sys.path.insert(0, str(ROOT / "tests" / "integration"))
from e2e_x11 import check, rect_eq, results  # noqa: E402
from gui_x11 import GuiEnv  # noqa: E402

SCREENS = "1600x1200+1280x1024"


def run_all(e: GuiEnv) -> None:
    e.start_daemon()
    e.run("config", "set", "general.panel_shortcut", '"Ctrl+Alt+G"')
    e.run("config", "set", "general.first_run_complete", "true")
    outs = e.cli_json("list-outputs")
    if not check("G22a", "two monitors are seen", len(outs) == 2, f"{[(o['stable_id'], o['rect']) for o in outs]}"):
        return
    e.window("gtk4", "org.fractile.Editor", "Editor window")
    ed = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Editor")
    if not check("G22b", "test window is up", bool(ed)):
        return
    wid = ed["id"]
    xid = int(wid.split(":")[1], 16)
    e.geom.unlink(missing_ok=True)
    e.xdo("windowactivate", "--sync", str(xid))
    time.sleep(0.3)
    e.xdo("key", "ctrl+alt+g")
    geo = e.panel_geometry(8.0) or []
    time.sleep(1.5)  # let any move after the map settle (see position_own)

    def inside(p, o) -> bool:
        r = o["rect"]
        cx, cy = p["x"] + p["w"] // 2, p["y"] + p["h"] // 2
        return r["x"] <= cx < r["x"] + r["w"] and r["y"] <= cy < r["y"] + r["h"]

    by_out = {o["stable_id"]: o for o in outs}
    placed_right = len(geo) == 2 and all(p["output"] in by_out and inside(p, by_out[p["output"]]) for p in geo)
    check("G22", "one panel per monitor, each on its own monitor", placed_right, f"{[(p['output'], p['x'], p['y']) for p in geo]}")
    home = next((o for o in outs if inside({"x": ed["frame"]["x"], "y": ed["frame"]["y"], "w": ed["frame"]["w"], "h": ed["frame"]["h"]}, o)), outs[0])
    other = next((p for p in geo if p["output"] != home["stable_id"]), None)
    if not other:
        check("G23", "dragging on the other monitor's panel places the window on that monitor exactly", False, "no panel for the other monitor")
        return
    x0, y0 = e.cell_center(other, 1, 1)
    x1, y1 = e.cell_center(other, 3, 4)
    e.xdo("mousemove", str(x0), str(y0), "mousedown", "1")
    time.sleep(0.15)
    e.xdo("mousemove", str(x1), str(y1))
    time.sleep(0.3)
    e.xdo("mouseup", "1")
    c = e.client()
    try:
        want = c.call("compute_rect", {"output": other["output"], "grid": {"cols": other["cols"], "rows": other["rows"]},
                                       "selection": {"col0": 1, "row0": 1, "col1": 3, "row1": 4}})
    finally:
        c.close()
    got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want) else None)(e.frame(wid)), 4.0) or e.frame(wid)
    check("G23", "dragging on the other monitor's panel places the window on that monitor exactly",
          rect_eq(got, want) and inside(got, by_out[other["output"]]), f"want {want} got {got}")
    if e.visible_named("Fractile panel"):
        e.xdo("key", "Escape")
        time.sleep(0.5)

    # G25: pressing the same placement shortcut repeatedly cycles the window
    # across the monitors (DV-17; M04 on GNOME Wayland)
    c = e.client()
    try:
        cfg = c.call("get_config")["config"]
        cfg["shortcuts"].append({"id": "t-left-third", "name": "Left third", "scope": "global", "accelerator": "Ctrl+Alt+1", "cycle_monitors": True,
                                 "action": {"type": "place", "grid": {"cols": 3, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}})
        r = c.call("set_config", {"config": cfg})
        t = {o["stable_id"]: c.call("compute_rect", {"output": o["stable_id"], "grid": {"cols": 3, "rows": 1},
                                                    "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}) for o in outs}
    finally:
        c.close()
    a, b = home["stable_id"], next(o["stable_id"] for o in outs if o["stable_id"] != home["stable_id"])
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", wid, "--output", a)
    seen = []
    for want_out in (a, b, a):
        e.xdo("windowactivate", "--sync", str(xid))
        time.sleep(0.3)
        e.xdo("key", "ctrl+alt+1")
        hit = e.wait_for(lambda w=want_out: rect_eq(e.frame(wid), t[w]), 3.0, 0.05)
        seen.append(want_out if hit else f"miss:{e.frame(wid)}")
    check("G25", "pressing the same placement shortcut repeatedly cycles the window across monitors",
          not r.get("hotkey_errors") and seen == [a, b, a], " -> ".join(seen) + (f"; hotkey errors {r.get('hotkey_errors')}" if r.get("hotkey_errors") else ""))

    log = e.ui_log.read_text() if e.ui_log.exists() else ""
    tb = [line for line in log.splitlines() if "Traceback" in line or "Error" in line]
    check("G24", "UI logged no Python errors", not tb, "; ".join(tb[:3]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", type=int, default=43)
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "gui-two-monitors.json"))
    a = ap.parse_args()
    nested = ROOT / "tests" / "integration" / "nested-gnome-x11.sh"
    st = subprocess.run(["bash", str(nested), "start", str(a.display), SCREENS], capture_output=True, text=True)
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
        subprocess.run(["bash", str(nested), "stop", str(a.display)])
    wm = "kwin_x11" if os.environ.get("FRACTILE_TEST_WM") == "kwin" else "gnome-shell"
    server = "Xvfb split into two RandR monitors" if os.environ.get("FRACTILE_TEST_XSERVER") == "xvfb" else "Xephyr with two screens"
    ev = {"suite": "gui-two-monitors", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "environment": f"{server} {SCREENS}, {wm}, PyQt6 UI", "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
