#!/usr/bin/env python3
"""Monitor changes at runtime on headless GNOME Shell Wayland: mixed DPI
(T08) and monitor disconnect/reconnect (T13).

Two virtual monitors (1600x1200 and 1280x1024). The monitor configuration is
changed while everything runs, through Mutter's org.gnome.Mutter.DisplayConfig
ApplyMonitorsConfig (the call GNOME Settings > Displays makes):
  - mixed DPI: the 1600x1200 monitor at scale 2, the other at scale 1, in
    whichever layout mode Mutter runs in: physical (upstream default) or
    logical (when scale-monitor-framebuffer is on, as Fedora ships it). The
    mode is recorded in D01;
  - disconnect: the second monitor disabled, then enabled again. This is the
    compositor-side event an unplug produces (outputs removed, windows moved
    by Mutter, monitors-changed). It is not a physical unplug: the connector
    stays, and no hardware hot-plug detection is involved.

Usage: tests/integration/e2e_gnome_wayland_displays.py [--keep] [--evidence FILE]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_gnome_wayland as base  # noqa: E402
from e2e_gnome_wayland import EXT_UUID, ROOT, Env, check, expected, rect_eq, results  # noqa: E402

MONITORS = "1600x1200,1280x1024"
DC = "org.gnome.Mutter.DisplayConfig"
MODES = {"Meta-0": "1600x1200@60.000", "Meta-1": "1280x1024@60.000"}


def gdbus(e: Env, method: str, *args: str) -> str:
    p = subprocess.run(["gdbus", "call", "--session", "--dest", DC, "--object-path", "/org/gnome/Mutter/DisplayConfig", "--method", f"{DC}.{method}", *args],
                       env=e.env, capture_output=True, text=True, timeout=10)
    if p.returncode != 0:
        raise RuntimeError(f"{method}: {p.stderr.strip()}")
    return p.stdout


def layout_mode(e: Env) -> str:
    """Mutter's layout mode: "logical" (positions in scaled pixels) or "physical"."""
    m = re.search(r"'layout-mode': <uint32 (\d+)>", gdbus(e, "GetCurrentState"))
    return "logical" if m and m.group(1) == "1" else "physical"


def right_of(e: Env, width: int, scale: float) -> int:
    """x of a monitor placed right of one `width` physical pixels wide at `scale`."""
    return round(width / scale) if layout_mode(e) == "logical" else width


def apply_monitors(e: Env, layout: list[tuple[str, int, int, float, bool]]) -> None:
    """layout: (connector, x, y, scale, primary). Monitors not listed are disabled."""
    serial = re.match(r"\(uint32 (\d+),", gdbus(e, "GetCurrentState")).group(1)
    lms = ", ".join(f"({x}, {y}, {scale}, uint32 0, {'true' if prim else 'false'}, [('{c}', '{MODES[c]}', @a{{sv}} {{}})])" for c, x, y, scale, prim in layout)
    gdbus(e, "ApplyMonitorsConfig", serial, "1", f"[{lms}]", "@a{sv} {}")


def outputs(e: Env) -> list[dict]:
    return sorted(e.cli_json("list-outputs"), key=lambda o: o["rect"]["x"])


def inside(r, wa):
    return r["x"] >= wa["x"] and r["y"] >= wa["y"] and r["x"] + r["w"] <= wa["x"] + wa["w"] and r["y"] + r["h"] <= wa["y"] + wa["h"]


def norm_on(n, wa):
    return {"x": round(wa["x"] + n[0] * wa["w"]), "y": round(wa["y"] + n[1] * wa["h"]), "w": round(n[2] * wa["w"]), "h": round(n[3] * wa["h"])}


def run_all(e: Env) -> None:
    e.start_daemon()
    e.wait_for(lambda: e.status()["window_control"], 10.0)
    e.window("gtk4", "org.fractile.TestGtk", "Doc G")
    e.window("qt", "org.fractile.TestQt", "Doc Q")
    g = e.wait_window(lambda w: w["app_id"] == "org.fractile.TestGtk", 15.0)["id"]
    q = e.wait_window(lambda w: w["app_id"] == "org.fractile.TestQt", 15.0)["id"]
    outs0 = outputs(e)
    if not check("D00", "two monitors and two windows are up", len(outs0) == 2 and g and q, str([o["stable_id"] for o in outs0])):
        return
    ida, idb = outs0[0]["stable_id"], outs0[1]["stable_id"]

    # ---- T08: mixed DPI ----
    mode = layout_mode(e)
    apply_monitors(e, [("Meta-0", 0, 0, 2.0, True), ("Meta-1", right_of(e, 1600, 2.0), 0, 1.0, False)])
    mixed = e.wait_for(lambda: (lambda os_: os_ if len(os_) == 2 and sorted(o["scale"] for o in os_) == [1.0, 2.0] else None)(outputs(e)), 10.0, 0.3)
    check("D01", "monitor scale changed at runtime to 2 and 1: Fractile reports both scales, same stable ids",
          bool(mixed) and [o["stable_id"] for o in mixed] == [ida, idb],
          f"Mutter layout mode {mode}; " + "; ".join(f"{o['stable_id']} scale {o['scale']} rect {o['rect']} work {o['work_area']}" for o in (mixed or outputs(e))))
    if not mixed:
        return
    rnd = random.Random(8)
    bad, constrained = [], 0
    for i in range(100):
        o = mixed[i % 2]["stable_id"]
        cols, rows = rnd.randint(1, 10), rnd.randint(1, 10)
        c0, r0 = rnd.randint(0, cols - 1), rnd.randint(0, rows - 1)
        c1, r1 = rnd.randint(c0, cols - 1), rnd.randint(r0, rows - 1)
        w = g if i % 3 else q
        out = e.cli_json("place", "--grid", f"{cols}x{rows}", "--cells", f"{c0},{r0},{c1},{r1}", "--window", w, "--output", o)
        constrained += "constrained by the window" in (out.get("note") or "")
        want = expected(e, cols, rows, c0, r0, c1, r1, o)
        exact = rect_eq(out.get("observed"), want, 2) or "constrained by the window" in (out.get("note") or "")
        if not out.get("within_tolerance") or not exact:
            bad.append(f"{i}:{cols}x{rows}:{c0},{r0},{c1},{r1}@{o} observed {out.get('observed')} want {want} {out.get('note') or ''}")
    alive = e.daemon and e.daemon.poll() is None
    final = e.cli_json("place", "--grid", "6x6", "--cells", "0,0,2,5", "--window", g, "--output", ida)["observed"]
    check("D02", "100 placements alternating a scale-2 and a scale-1 monitor: each exact (or fitted to the window minimum and reported), no drift, service alive",
          not bad and alive and rect_eq(final, expected(e, 6, 6, 0, 0, 2, 5, ida), 2),
          f"{constrained} fitted to the window minimum" + (f"; off: {bad[:4]}" if bad else ""))
    e.cli_json("place", "--grid", "2x1", "--cells", "1,0,1,0", "--window", q, "--output", idb)
    want_g, want_q = e.frame(g), e.frame(q)
    e.cli_json("save", "--name", "mixed")
    e.cli_json("move-output", "next", "--window", g)
    e.cli_json("move-output", "next", "--window", q)
    rep = e.cli_json("restore", "--snapshot", "mixed")
    check("D03", "snapshot restore across the scale-2 and scale-1 monitors puts both windows back exactly",
          rep["state"] == "completed" and rect_eq(e.frame(g), want_g, 2) and rect_eq(e.frame(q), want_q, 2), f"{e.frame(g)} {e.frame(q)} want {want_g} {want_q}")

    # back to scale 1 for the disconnect checks
    apply_monitors(e, [("Meta-0", 0, 0, 1.0, True), ("Meta-1", 1600, 0, 1.0, False)])
    e.wait_for(lambda: sorted(o["scale"] for o in outputs(e)) == [1.0, 1.0], 10.0, 0.3)

    # ---- T13: disconnect and reconnect ----
    e.run("config", "set", "session.restore_on_monitor_reconnect", "true")
    e.cli_json("place", "--grid", "2x2", "--cells", "1,0,1,0", "--window", g, "--output", idb)
    e.cli_json("place", "--grid", "2x1", "--cells", "0,0,0,0", "--window", q, "--output", ida)
    home_g, home_q = e.frame(g), e.frame(q)
    e.cli_json("save", "--name", "two-monitors")
    snap = e.cli_json("snapshot", "export", "two-monitors")
    rec_g = next(r for r in snap["windows"] if r.get("app_id") == "org.fractile.TestGtk")
    # the layout moves on after the last saved snapshot: the return must use
    # what the setup had when the monitor went, not an older snapshot
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", g, "--output", idb)
    home_g = e.frame(g)
    time.sleep(11)  # the service remembers the current setup's layout every 10 s
    apply_monitors(e, [("Meta-0", 0, 0, 1.0, True)])
    one = e.wait_for(lambda: (lambda os_: os_ if len(os_) == 1 else None)(outputs(e)), 10.0, 0.3)
    wa_a = one[0]["work_area"] if one else None
    moved_in = bool(one) and e.wait_for(lambda: inside(e.frame(g), wa_a), 5.0, 0.2)
    check("D04", "second monitor disconnected: Fractile sees one monitor, and the window from it is now on the remaining one",
          bool(one) and one[0]["stable_id"] == ida and moved_in, f"outputs {[o['stable_id'] for o in (one or [])]}; window {e.frame(g)}")
    plan = e.cli_json("restore", "--dry-run", "--snapshot", "two-monitors")
    warn = " ".join(plan.get("warnings") or [])
    rep = e.cli_json("restore", "--snapshot", "two-monitors", "--no-launch")
    want_fallback = norm_on(rec_g["normalized_geometry"], wa_a) if wa_a else None
    check("D05", "restoring a two-monitor snapshot with one monitor: the missing monitor's window goes to the remaining one at the same relative position",
          idb in warn and rect_eq(e.frame(g), want_fallback, 3) and rect_eq(e.frame(q), home_q, 2),
          f"warning: {warn[:160]}; window {e.frame(g)} want {want_fallback}; state {rep.get('state')}")
    kept = []
    for s in e.cli_json("list-snapshots", "--limit", "50"):
        if s.get("mode") == "topology":
            x = e.cli_json("snapshot", "export", s["id"])
            rg = next((r for r in x["windows"] if r.get("app_id") == "org.fractile.TestGtk"), None)
            if len(x["outputs"]) == 2 and rg and rect_eq(rg["absolute_geometry"], home_g, 2):
                kept.append(s["id"])
    check("D06", "the layout the two-monitor setup had is kept as a 'monitor setup' snapshot when the monitor goes",
          len(kept) == 1, f"{len(kept)} matching topology snapshots")

    apply_monitors(e, [("Meta-0", 0, 0, 1.0, True), ("Meta-1", 1600, 0, 1.0, False)])
    two = e.wait_for(lambda: (lambda os_: os_ if len(os_) == 2 else None)(outputs(e)), 10.0, 0.3)
    back = e.wait_for(lambda: rect_eq(e.frame(g), home_g, 2) and rect_eq(e.frame(q), home_q, 2), 15.0, 0.3)
    check("D07", "monitor reconnected (return enabled): same stable id, and both windows go back to where they were on the two-monitor setup",
          bool(two) and [o["stable_id"] for o in two] == [ida, idb] and bool(back), f"outputs {[o['stable_id'] for o in (two or [])]}; g {e.frame(g)} want {home_g}; q {e.frame(q)} want {home_q}")

    # with the return turned off (the default), a reconnect moves nothing
    e.run("config", "set", "session.restore_on_monitor_reconnect", "false")
    time.sleep(11)
    apply_monitors(e, [("Meta-0", 0, 0, 1.0, True)])
    e.wait_for(lambda: len(outputs(e)) == 1, 10.0, 0.3)
    e.wait_for(lambda: inside(e.frame(g), outputs(e)[0]["work_area"]), 5.0, 0.2)
    e.cli_json("snap", "center", "--window", g)
    parked = e.frame(g)
    apply_monitors(e, [("Meta-0", 0, 0, 1.0, True), ("Meta-1", 1600, 0, 1.0, False)])
    e.wait_for(lambda: len(outputs(e)) == 2, 10.0, 0.3)
    time.sleep(3)
    check("D08", "with the return turned off (the default), reconnecting the monitor moves no window",
          rect_eq(e.frame(g), parked, 2), f"{e.frame(g)} want {parked}")
    alive = e.daemon and e.daemon.poll() is None
    tb = [ln for ln in (e.ui_log.read_text().splitlines() if e.ui_log.exists() else []) if "Traceback" in ln]
    check("D09", "service alive through all monitor changes; no UI errors", alive and not tb, "; ".join(tb[:2]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "e2e-gnome-wayland-displays.json"))
    a = ap.parse_args()
    e = Env(MONITORS)
    try:
        run_all(e)
    except Exception as ex:  # report, don't hide
        check("DXX", "suite crashed", False, repr(ex))
    finally:
        if any(not r["ok"] for r in results) and (e.base / "daemon.out").exists():
            print("== daemon log (monitor and restore lines)")
            print("\n".join([ln for ln in (e.base / "daemon.out").read_text().splitlines() if any(k in ln for k in (" Warn ", " Error ", "monitor", "restore "))][-30:]))
        e.cleanup(a.keep)
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "e2e-gnome-wayland-displays", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"headless {gs} --wayland --no-x11, virtual monitors {MONITORS}, changed at runtime through Mutter DisplayConfig (scale 2+1, disable/enable); Fractile extension {EXT_UUID}",
          "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    base.NAME = f"fwl-disp-{base.os.getpid()}"
    sys.exit(main())
