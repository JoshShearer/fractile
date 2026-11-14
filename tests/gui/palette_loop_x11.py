#!/usr/bin/env python3
"""Regression test for G10's intermittent failure under kwin_x11: the
command palette's snap acted on "the active window" after the palette had
closed, and KWin sometimes reports no active window for a moment then, so
the snap failed (about 1 run in 35). The palette now sends the window that
had focus when it opened. Repeats the palette's "left half" N times.
  tests/gui/gui-kwin-x11-in-container.sh palette_loop_x11.py palette-loop-kwin.json
"""
import argparse, json, os, subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/integration")); sys.path.insert(0, str(ROOT / "tests/gui"))
from gui_x11 import GuiEnv, expected
from e2e_x11 import rect_eq
ap = argparse.ArgumentParser()
ap.add_argument("--display", type=int, default=41)
ap.add_argument("--evidence")
ap.add_argument("--runs", type=int, default=int(os.environ.get("FRACTILE_LOOP_RUNS", "60")))
a = ap.parse_args()
N = a.runs
nested = ROOT / "tests/integration/nested-gnome-x11.sh"
st = subprocess.run(["bash", str(nested), "start", str(a.display), "1600x1200"], capture_output=True, text=True)
assert st.returncode == 0, st.stdout + st.stderr
e = GuiEnv(a.display)
fails = 0
try:
    e.start_daemon()
    e.run("config", "set", "general.first_run_complete", "true")
    e.run("config", "set", "diagnostics.log_level", '"debug"')
    e.window("gtk4", "org.fractile.Editor", "Editor window")
    e.window("qt", "org.fractile.Viewer", "Viewer window")
    ed = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Editor")
    e.wait_window(lambda w: w.get("title") == "Viewer window")
    wid = ed["id"]; xid = int(wid.split(":")[1], 16)
    want = expected(e, 2, 1, 0, 0, 0, 0)
    for i in range(N):
        e.cli_json("snap", "right-half", "--window", wid)
        e.xdo("windowactivate", "--sync", str(xid)); time.sleep(0.3)
        e.run("palette")
        pal = e.wait_for(lambda: e.visible_named("Fractile command palette"), 8.0)
        time.sleep(0.4)
        act_before = e.active_xid()
        e.xdo("type", "--delay", "30", "left half"); time.sleep(0.3)
        e.xdo("key", "Return")
        got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want) else None)(e.frame(wid)), 4.0) or e.frame(wid)
        ok = bool(pal) and rect_eq(got, want)
        if not ok:
            fails += 1
            log = (e.base / "daemon.out").read_text().splitlines()
            print(f"run {i}: FAIL pal={pal} active_before_typing={hex(act_before or 0)} pal_xids={[hex(x) for x in pal or []]} editor={hex(xid)} got={got}")
            print("   daemon:", [l for l in log[-40:] if "place " not in l][-8:])
            print("   still visible:", e.visible_named("Fractile command palette"))
            e.xdo("key", "Escape"); time.sleep(0.5)
        else:
            print(f"run {i}: ok")
        time.sleep(0.5)
finally:
    print(f"FAILS {fails}/{N}")
    for p in e.base.glob("*.log"):
        t = p.read_text(errors="replace")
        if "Traceback" in t or "rror" in t: print(p.name, t[-1500:])
    e.cleanup()
    subprocess.run(["bash", str(nested), "stop", str(a.display)])
    if a.evidence:
        ok = fails == 0
        Path(a.evidence).write_text(json.dumps({"suite": "palette-loop-x11", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "results": [{"id": "G10L", "name": f"command palette 'left half' snaps the focused window in {N} of {N} runs", "ok": ok,
                         "detail": f"{fails} failed"}]}, indent=2) + "\n")
sys.exit(1 if fails else 0)
