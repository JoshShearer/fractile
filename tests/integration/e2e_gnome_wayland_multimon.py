#!/usr/bin/env python3
"""Multi-monitor end-to-end tests on headless GNOME Shell Wayland: two
virtual monitors of different sizes (1600x1200 and 1280x1024, left to
right), the Fractile extension, native Wayland GTK4/Qt windows and the real
Fractile UI. Shortcuts are real key events injected by the test harness
(tests/tools/wlkeys.py); Fractile never injects input.

Covers SRS T07/T08 (mixed *sizes*; the virtual monitors are all scale 1, so
mixed DPI is not covered) and DV-12, DV-13, DV-17.

Usage: tests/integration/e2e_gnome_wayland_multimon.py [--keep] [--evidence FILE]
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_gnome_wayland as base  # noqa: E402
from e2e_gnome_wayland import EXT_UUID, ROOT, Env, check, expected, rect_eq, results  # noqa: E402

MONITORS = "1600x1200,1280x1024"


def inside(r, wa):
    return r["x"] >= wa["x"] and r["y"] >= wa["y"] and r["x"] + r["w"] <= wa["x"] + wa["w"] and r["y"] + r["h"] <= wa["y"] + wa["h"]


def run_all(e: Env) -> None:
    e.start_daemon()
    e.wait_for(lambda: e.status()["window_control"], 10.0)
    outs = e.cli_json("list-outputs")
    outs.sort(key=lambda o: o["rect"]["x"])
    ids = [o["stable_id"] for o in outs]
    ok = len(outs) == 2 and len(set(ids)) == 2 and outs[1]["rect"]["x"] >= outs[0]["rect"]["x"] + outs[0]["rect"]["w"]
    check("M01", "two monitors listed with distinct stable ids, left to right", ok, "; ".join(f"{o['stable_id']} {o['rect']}" for o in outs))
    if len(outs) != 2:
        return
    a, b = outs
    wa = {o["stable_id"]: o["work_area"] for o in outs}

    e.window("gtk4", "org.fractile.TestGtk", "Doc G")
    e.window("qt", "org.fractile.TestQt", "Doc Q")
    g = e.wait_window(lambda w: w["app_id"] == "org.fractile.TestGtk", 15.0)
    q = e.wait_window(lambda w: w["app_id"] == "org.fractile.TestQt", 15.0)
    g, q = g["id"], q["id"]

    # Placement onto the other monitor's grid (DV-13)
    want = expected(e, 2, 2, 1, 1, 1, 1, b["stable_id"])
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", g, "--output", b["stable_id"])
    fr = e.frame(g)
    check("M02", "a grid selection on the second monitor places the window there exactly", rect_eq(fr, want, 2) and inside(fr, wa[b["stable_id"]]), f"{fr} want {want}")

    # Move to next monitor keeps the relative rectangle; order is stable
    e.cli_json("place", "--grid", "4x4", "--cells", "0,0,1,1", "--window", q, "--output", a["stable_id"])
    seq = []
    for _ in range(3):
        e.cli_json("move-output", "next", "--window", q)
        fr = e.frame(q)
        seq.append(next((o["stable_id"] for o in outs if inside(fr, o["work_area"])), "?"))
    rel_ok = rect_eq(e.frame(q), expected(e, 4, 4, 0, 0, 1, 1, b["stable_id"]), 2)
    check("M03", "move to next monitor cycles in a stable order and keeps the relative placement",
          seq == [b["stable_id"], a["stable_id"], b["stable_id"]] and rel_ok, " -> ".join(seq))

    # Repeated global shortcut cycles the window across monitors (DV-17)
    c = e.client()
    cfg = c.call("get_config")["config"]
    cfg["general"]["first_run_complete"] = True
    cfg["general"]["panel_shortcut"] = "Ctrl+Alt+G"
    cfg["general"]["panel_on_all_monitors"] = True
    cfg["shortcuts"].append({"id": "t-left-third", "name": "Left third", "scope": "global", "accelerator": "Ctrl+Alt+1", "cycle_monitors": True,
                             "action": {"type": "place", "grid": {"cols": 3, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}})
    r = c.call("set_config", {"config": cfg})
    c.close()
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", g, "--output", a["stable_id"])
    e.activate(g)
    t = {o["stable_id"]: expected(e, 3, 1, 0, 0, 0, 0, o["stable_id"]) for o in outs}
    seen = []
    for want_out in (a["stable_id"], b["stable_id"], a["stable_id"]):
        e.keys("ctrl+alt+1")
        hit = e.wait_for(lambda w=want_out: rect_eq(e.frame(g), t[w], 3), 3.0, 0.05)
        seen.append(want_out if hit else f"miss:{e.frame(g)}")
    check("M04", "pressing the same placement shortcut repeatedly cycles the window across monitors",
          not r["hotkey_errors"] and seen == [a["stable_id"], b["stable_id"], a["stable_id"]], " -> ".join(seen))

    # Panel on each monitor (DV-12)
    e.activate(q)
    e.keys("ctrl+alt+g")
    panels = e.wait_for(lambda: (lambda ps: ps if len(ps) == 2 else None)([w for w in e.windows(True) if w["title"].startswith("Fractile panel")]), 12.0, 0.3) or []
    where = sorted(next((o["stable_id"] for o in outs if inside(p["frame"], o["work_area"])), "?") for p in panels)
    check("M05", "the panel shortcut shows one panel inside each monitor's work area", where == sorted(ids) and all(p["is_own"] for p in panels),
          f"{len(panels)} panels on {where}")
    c = e.client()
    c.call("hide_panel")
    c.close()

    # 100 placements alternating monitors and grids: no drift, no failure (T08)
    rnd = random.Random(7)
    bad = []
    constrained = 0
    t0 = time.time()
    for i in range(100):
        o = outs[i % 2]["stable_id"]
        cols, rows = rnd.randint(1, 10), rnd.randint(1, 10)
        c0, r0 = rnd.randint(0, cols - 1), rnd.randint(0, rows - 1)
        c1, r1 = rnd.randint(c0, cols - 1), rnd.randint(r0, rows - 1)
        w = g if i % 3 else q
        out = e.cli_json("place", "--grid", f"{cols}x{rows}", "--cells", f"{c0},{r0},{c1},{r1}", "--window", w, "--output", o)
        constrained += "constrained by the window" in (out.get("note") or "")
        if not out.get("within_tolerance"):
            bad.append(f"{i}:{cols}x{rows}:{c0},{r0},{c1},{r1}@{o} err={out.get('max_error_px')} {out.get('note') or ''}")
    el = time.time() - t0
    alive = e.daemon and e.daemon.poll() is None
    # back to a known cell after the run: still exact
    final_ok = rect_eq(e.cli_json("place", "--grid", "6x6", "--cells", "0,0,2,5", "--window", g, "--output", a["stable_id"])["observed"],
                       expected(e, 6, 6, 0, 0, 2, 5, a["stable_id"]), 2)
    check("M06", "100 placements alternating monitors and grid sizes: each exact (or fitted to the window minimum size and reported), no drift, service alive",
          not bad and alive and final_ok, f"{el:.1f}s total; {constrained} hit the window's minimum size and were fitted" + (f"; off: {bad[:5]}" if bad else ""))

    # Snapshot restore across monitors
    e.cli_json("place", "--grid", "2x1", "--cells", "1,0,1,0", "--window", q, "--output", b["stable_id"])
    want_g, want_q = e.frame(g), e.frame(q)
    e.cli_json("save", "--name", "multimon")
    e.cli_json("move-output", "next", "--window", g)
    e.cli_json("move-output", "next", "--window", q)
    rep = e.cli_json("restore", "--snapshot", "multimon")
    check("M07", "snapshot restore puts windows back on their own monitors exactly",
          rep["state"] == "completed" and rect_eq(e.frame(g), want_g, 2) and rect_eq(e.frame(q), want_q, 2), f"{e.frame(g)} {e.frame(q)}")

    tb = [ln for ln in (e.ui_log.read_text().splitlines() if e.ui_log.exists() else []) if "Traceback" in ln]
    check("M08", "UI logged no Python errors", not tb, "; ".join(tb[:2]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "e2e-gnome-wayland-multimon.json"))
    a = ap.parse_args()
    e = Env(MONITORS)
    try:
        run_all(e)
    except Exception as ex:  # report, don't hide
        check("MXX", "suite crashed", False, repr(ex))
    finally:
        if any(not r["ok"] for r in results) and (e.base / "daemon.out").exists():
            print("== daemon log (warnings)")
            print("\n".join([ln for ln in (e.base / "daemon.out").read_text().splitlines() if " Warn " in ln or " Error " in ln][-20:]))
        e.cleanup(a.keep)
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "e2e-gnome-wayland-multimon", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"headless {gs} --wayland --no-x11, virtual monitors {MONITORS} (scale 1), Fractile extension {EXT_UUID}", "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    base.NAME = f"fwl-mm-{base.os.getpid()}"
    sys.exit(main())
