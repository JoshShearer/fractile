#!/usr/bin/env python3
"""Settings and shortcut-editor checks (DV-25, DV-26) on nested GNOME X11.

Starts an isolated nested GNOME Shell (X11) and fractiled. Another X client
grabs Ctrl+Alt+Shift+K, as a desktop shortcut would. settings_probe.py then runs the
real Settings window against the live service and types into the shortcut
editor with real X key events. Finally the shortcut created in Settings is
pressed (real key event) and the window geometry is checked.

Usage: tests/gui/settings_x11.py [--display 23] [--keep] [--evidence FILE]
Needs: Xephyr, gnome-shell, xdotool, python3-pyqt6, python3-xlib.
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
import pause_checks  # noqa: E402
sys.path.insert(0, str(ROOT / "tests" / "gui"))
from e2e_x11 import check, rect_eq, results  # noqa: E402
from gui_x11 import SHOTS, GuiEnv  # noqa: E402

# Holds a passive grab on Ctrl+Alt+Shift+K on the root window until killed.
# (Not an F-key: Ctrl+Alt+F<n> is the X server's VT switch.)
GRABBER = """
import sys, time
from Xlib import X, XK, display
d = display.Display()
root = d.screen().root
kc = d.keysym_to_keycode(XK.string_to_keysym("k"))
for extra in (0, X.Mod2Mask):
    root.grab_key(kc, X.ControlMask | X.Mod1Mask | X.ShiftMask | extra, True, X.GrabModeAsync, X.GrabModeAsync)
d.sync()
print("grabbed", flush=True)
while True:
    time.sleep(1)
"""


def run_all(e: GuiEnv) -> None:
    e.start_daemon()
    e.run("config", "set", "general.panel_shortcut", '"Ctrl+Alt+G"')
    e.run("config", "set", "general.first_run_complete", "true")
    e.window("gtk4", "org.fractile.Editor", "Editor window")
    ed = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Editor")
    grab = subprocess.Popen([sys.executable, "-c", GRABBER], env=e.env, stdout=subprocess.PIPE, text=True)
    e.procs.append(grab)
    grabbed = grab.stdout.readline().strip() == "grabbed"
    if not check("S00", "test window is up and another client holds Ctrl+Alt+Shift+K", bool(ed) and grabbed):
        return
    probe = subprocess.run([sys.executable, str(ROOT / "tests" / "gui" / "settings_probe.py"), str(SHOTS)],
                           env=e.env, capture_output=True, text=True, timeout=180)
    try:
        out = json.loads(probe.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        check("SXX", "probe produced results", False, (probe.stdout + probe.stderr)[-800:])
        return
    for r in out["results"]:
        results.append(r)
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['id']}: {r['name']}" + (f"  ({r['detail']})" if r["detail"] else ""))
    if probe.stderr.strip():
        print(probe.stderr[-1500:])
    check("S12", "the UI logged no Python errors", "Traceback" not in probe.stderr, probe.stderr[-300:])

    # S11: the shortcut made in Settings works for real.
    wid = ed["id"]
    xid = int(wid.split(":")[1], 16)
    e.xdo("windowmove", str(xid), "900", "500")
    time.sleep(0.5)
    e.xdo("windowactivate", str(xid))
    time.sleep(0.5)
    e.xdo("key", "ctrl+alt+shift+F9")
    wa = e.cli_json("list-outputs")[0]["work_area"]
    want = {"x": wa["x"], "y": wa["y"], "w": wa["w"] // 2, "h": wa["h"]}
    got = e.wait_for(lambda: (f := e.frame(wid)) and rect_eq(f, want, 2) and f, 8.0)
    check("S11", "pressing the shortcut created in Settings places the window (left half)", bool(got), f"want {want} got {e.frame(wid)}")
    pause_checks.run(Path(e.env["FRACTILE_RUNTIME_DIR"]) / "fractile.sock", lambda: e.cli_json("status"), check)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", type=int, default=23)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "settings-x11.json"))
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
        check("SXX", "suite crashed", False, repr(ex))
    finally:
        e.cleanup()
        if not a.keep:
            subprocess.run(["bash", str(nested), "stop", str(a.display)])
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "settings-x11", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "environment": f"Xephyr nested {gs} (X11), PyQt6 UI",
          "screenshots": "tests/evidence/gui/", "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
