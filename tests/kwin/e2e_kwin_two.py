#!/usr/bin/env python3
"""Two-monitor checks on KWin Wayland (DV-12, DV-13, DV-17): kwin_wayland
--virtual --output-count 2 in the test container, the Fractile KWin script,
an isolated fractiled and the real PyQt6 UI on Wayland.

Shortcuts go through KGlobalAccel's invokeShortcut (the container has no
keyboard); the drag on the second panel uses KWin's fake-input pointer
(tests/tools/kwin_fake_input.py, test harness only). Fractile itself never
injects input.

  tests/kwin/e2e-kwin-in-container.sh e2e_kwin_two.py 2 e2e-kwin-wayland-two-monitors.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_kwin import ROOT, Env, check, invoke_shortcut, load_script, rect_eq, results  # noqa: E402

POINTER = ROOT / "tests" / "tools" / "kwin_fake_input.py"


def inside(r, o) -> bool:
    cx, cy = r["x"] + r["w"] // 2, r["y"] + r["h"] // 2
    return o["x"] <= cx < o["x"] + o["w"] and o["y"] <= cy < o["y"] + o["h"]


class Pointer:
    def __init__(self, env):
        self.p = subprocess.Popen([sys.executable, str(POINTER)], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        line = self.p.stdout.readline().strip()
        if line != "ready":
            raise RuntimeError(f"fake-input pointer did not start: {line!r} {self.p.stderr.read()[-300:] if self.p.poll() is not None else ''}")

    def send(self, cmd):
        self.p.stdin.write(cmd + "\n")
        self.p.stdin.flush()
        if self.p.stdout.readline().strip() != "ok":
            raise RuntimeError(f"pointer failed on {cmd!r}")

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(5)
        except Exception:  # noqa: BLE001
            self.p.kill()


def run_all(e: Env) -> None:
    geom = e.base / "panel-geometry.json"
    e.env["FRACTILE_UI_TEST_GEOMETRY"] = str(geom)
    e.start_daemon()
    load_script()
    e.wait_for(lambda: e.status()["window_control"], 15.0)
    outs = e.cli_json("list-outputs")
    if not check("KM00", "two outputs are seen through the KWin script", len(outs) == 2,
                 f"{[(o['stable_id'], o['rect']) for o in outs]}"):
        return
    by_out = {o["stable_id"]: o["rect"] for o in outs}
    cfg = e.call("get_config")["config"]
    cfg["general"]["first_run_complete"] = True
    cfg["general"]["panel_shortcut"] = "Ctrl+Alt+G"
    cfg["general"]["tray_icon"] = False
    cfg["shortcuts"].append({"id": "t-left-third", "name": "Left third", "scope": "global", "accelerator": "Ctrl+Alt+1", "cycle_monitors": True,
                             "action": {"type": "place", "grid": {"cols": 3, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}})
    e.call("set_config", {"config": cfg})
    e.window("qt", "org.fractile.TestQt", "Qt window")
    win = e.wait_for(lambda: next((w for w in e.windows() if w.get("title") == "Qt window"), None), 15.0, 0.3)
    if not check("KM01", "test window is up", bool(win)):
        return
    wid = win["id"]
    a = outs[0]["stable_id"]
    b = outs[1]["stable_id"]
    e.cli_json("place", "--grid", "2x2", "--cells", "0,0,0,0", "--window", wid, "--output", a)

    # DV-12: one panel per monitor
    def panels():
        return [w for w in e.windows(True) if w["title"].startswith("Fractile panel")]

    e.call("activate_window", {"window": wid})
    e.wait_for(lambda: e.call("active_window") == wid, 3.0)
    geom.unlink(missing_ok=True)
    invoke_shortcut("panel")
    grids = e.wait_for(lambda: (lambda d: d if d and len(d) == 2 else None)(json.loads(geom.read_text()) if geom.exists() and geom.stat().st_size else None), 15.0, 0.3) or []
    e.wait_for(lambda: len(panels()) == 2 or None, 8.0, 0.3)
    # the KWin script moves each panel onto its output after it maps: let the frames settle
    last, since, end = None, time.time(), time.time() + 6.0
    while time.time() < end and time.time() - since < 1.0:
        cur = sorted((p["frame"]["x"], p["frame"]["y"]) for p in panels())
        if cur != last:
            last, since = cur, time.time()
        time.sleep(0.2)
    pw = panels()
    matched = []
    for g in grids:
        o = by_out.get(g["output"])
        w = next((p for p in pw if o and inside(p["frame"], o)), None)
        if w:
            matched.append(dict(g, x=w["frame"]["x"] + g["ox"], y=w["frame"]["y"] + g["oy"]))
    check("KM02", "the panel shortcut opens one panel per monitor, each on its own monitor (KWin Wayland)",
          len(pw) == 2 and len(matched) == 2 and len({m["output"] for m in matched}) == 2,
          f"panels {[p['frame'] for p in pw]}; grids {[(g['output'], g.get('ox'), g.get('oy')) for g in grids]}")

    # DV-13: drag on the other monitor's panel
    other = next((m for m in matched if m["output"] == b), None)
    if other:
        ptr = Pointer(e.env)
        cw, ch = other["w"] / other["cols"], other["h"] / other["rows"]

        def cell(col, row):
            return other["x"] + (col + 0.5) * cw, other["y"] + (row + 0.5) * ch

        ax, ay = cell(1, 1)
        bx, by = cell(3, 4)
        ptr.send(f"abs {ax} {ay}")
        time.sleep(0.2)
        ptr.send("button down")
        time.sleep(0.2)
        for t in (0.33, 0.66, 1.0):
            ptr.send(f"abs {ax + (bx - ax) * t} {ay + (by - ay) * t}")
            time.sleep(0.15)
        time.sleep(0.5)
        prev = next((w["frame"] for w in e.windows(True) if w["title"].startswith("Fractile preview")), None)
        ptr.send("button up")
        ptr.close()
        want = e.call("compute_rect", {"output": b, "grid": {"cols": other["cols"], "rows": other["rows"]},
                                       "selection": {"col0": 1, "row0": 1, "col1": 3, "row1": 4}})
        got = e.wait_for(lambda: (lambda f: f if rect_eq(f, want, 2) else None)(e.frame(wid)), 5.0) or e.frame(wid)
        check("KM03", "dragging on the other monitor's panel places the window on that monitor exactly (KWin Wayland)",
              rect_eq(got, want, 2) and inside(got, by_out[b]), f"want {want} got {got}")
        check("KM04", "during that drag the preview window is on the other monitor at the exact target", rect_eq(prev, want, 3), f"preview {prev} want {want}")
    else:
        check("KM03", "dragging on the other monitor's panel places the window on that monitor exactly (KWin Wayland)", False, "no panel on the other monitor")
    e.wait_for(lambda: not panels() or None, 5.0, 0.3)

    # DV-17: the same placement shortcut repeated cycles the monitors
    t = {o: e.call("compute_rect", {"output": o, "grid": {"cols": 3, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}}) for o in (a, b)}
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", wid, "--output", a)
    seen = []
    for _ in range(3):
        e.call("activate_window", {"window": wid})
        e.wait_for(lambda: e.call("active_window") == wid, 3.0)
        invoke_shortcut("t-left-third")
        time.sleep(0.2)
        f = e.wait_for(lambda: (lambda fr: fr if any(rect_eq(fr, r, 3) for r in t.values()) and (not seen or not rect_eq(fr, t[seen[-1]], 3)) else None)(e.frame(wid)), 4.0) or e.frame(wid)
        seen.append(next((o for o, r in t.items() if rect_eq(f, r, 3)), "none"))
    check("KM05", "pressing the same placement shortcut again moves the window to the next monitor and back (a, b, a)",
          seen == [a, b, a], f"{seen}")
    log = e.ui_log.read_text(errors="replace") if e.ui_log.exists() else ""
    check("KM06", "the UI logged no Python errors", "Traceback" not in log, log[-300:])


def main() -> int:
    e = Env()
    try:
        run_all(e)
    except Exception as ex:  # noqa: BLE001
        check("KMXX", "suite crashed", False, repr(ex))
    finally:
        for pr in e.procs:
            pr.terminate()
        e.stop_daemon()
    kv = subprocess.run(["kwin_wayland", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "e2e-kwin-wayland-two-monitors", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"{kv} --virtual --output-count 2 (two 1600x1200 outputs side by side, Wayland, no Xwayland) in an archlinux container "
                         f"(render device: {os.environ.get('FRACTILE_TEST_GPU', 'none')}); Fractile KWin script; PyQt6 UI on Wayland; "
                         "pointer from KWin's fake-input protocol (test harness)",
          "results": results}
    Path(os.environ.get("EVIDENCE", "/tmp/e2e-kwin-two.json")).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    if failed and (e.base / "daemon.out").exists():
        print("== daemon log (warnings)")
        print("\n".join([ln for ln in (e.base / "daemon.out").read_text().splitlines() if " Warn " in ln or " Error " in ln][-25:]))
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
