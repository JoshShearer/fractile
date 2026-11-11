#!/usr/bin/env python3
"""Settings and shortcut-editor checks (DV-25, DV-26) on GNOME Shell Wayland.

Same probe as settings_x11.py (tests/gui/settings_probe.py), run against a
headless gnome-shell --wayland with the Fractile extension, the real PyQt6 UI
on Wayland (QT_QPA_PLATFORM=wayland) and a live fractiled. Keys are sent by
the test harness through Mutter's RemoteDesktop virtual keyboard
(tests/tools/wlkeys.py). Finally the shortcut created in Settings is pressed
and the window geometry is checked.

S05 differs from X11: on Wayland no other client can hold a key grab, so the
check uses a combination GNOME's own settings assign (Ctrl+Alt+Delete, log out).

Usage: tests/gui/settings_gnome_wayland.py [--keep] [--evidence FILE]
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
sys.path.insert(0, str(ROOT / "tests" / "gui"))
import pause_checks  # noqa: E402
from e2e_gnome_wayland import EXT_UUID, Env, check, rect_eq, results  # noqa: E402

SHOTS = Path(os.environ.get("FRACTILE_SHOT_DIR") or ROOT / "tests" / "evidence" / "gui" / "wayland")


def run_all(e: Env) -> None:
    e.start_daemon()
    e.wait_for(lambda: e.status()["window_control"], 10.0)
    e.run("config", "set", "general.panel_shortcut", '"Ctrl+Alt+G"')
    e.run("config", "set", "general.first_run_complete", "true")
    e.window("gtk4", "org.fractile.Editor", "Editor window")
    ed = e.wait_window(lambda w: w.get("app_id") == "org.fractile.Editor")
    st = e.status()
    if not check("S00", "test window is up and the GNOME integration is connected (Wayland)",
                 bool(ed) and st["adapter"] == "gnome" and st["session_type"] == "wayland", f"adapter={st['adapter']} session={st['session_type']}"):
        return
    SHOTS.mkdir(parents=True, exist_ok=True)
    probe = subprocess.run([sys.executable, str(ROOT / "tests" / "gui" / "settings_probe.py"), str(SHOTS)],
                           env=e.env, capture_output=True, text=True, timeout=300)
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

    wid = ed["id"]
    e.activate(wid)
    time.sleep(0.5)
    e.keys("ctrl+alt+shift+f9")
    wa = e.cli_json("list-outputs")[0]["work_area"]
    want = {"x": wa["x"], "y": wa["y"], "w": wa["w"] // 2, "h": wa["h"]}
    got = e.wait_for(lambda: (f := e.frame(wid)) and rect_eq(f, want, 2) and f, 8.0)
    check("S11", "pressing the shortcut created in Settings places the window (left half)", bool(got), f"want {want} got {e.frame(wid)}")
    pause_checks.run(e.sock, e.status, check)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "settings-gnome-wayland.json"))
    a = ap.parse_args()
    e = Env()
    try:
        run_all(e)
    except Exception as ex:  # noqa: BLE001
        check("SXX", "suite crashed", False, repr(ex))
    finally:
        e.cleanup(a.keep)
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "settings-gnome-wayland", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"headless {gs} --wayland --no-x11, Fractile extension {EXT_UUID}, PyQt6 UI on Wayland",
          "screenshots": "tests/evidence/gui/wayland/", "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
