#!/usr/bin/env python3
"""Restore when the desktop refuses the saved workspace (GitHub issue #1), on
a real KWin Wayland (kwin_wayland --virtual) through the Fractile KWin script.

A window is saved on workspace 3, then workspace 3 is removed. KWin rejects
moving the window to a workspace that does not exist, while it still accepts
the window's size and position. The restore must not report the window as
placed. After workspace 3 is created again, retrying the same run must move
the window there and report it placed.

  tests/kwin/e2e-kwin-in-container.sh e2e_kwin_ws_refused.py 1 e2e-kwin-wayland-ws-refused.json
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_kwin import Env, check, dbus, load_script, results  # noqa: E402

VDM = ("--dest=org.kde.KWin", "/VirtualDesktopManager")
SUCCESS = {"ALREADY_PRESENT", "LAUNCHED_AND_PLACED", "PLACED_ONLY", "RESTORED_WITHOUT_GEOMETRY", "AMBIGUOUS"}


def desktop_ids() -> list[str]:
    out = dbus(*VDM, "org.freedesktop.DBus.Properties.Get", "string:org.kde.KWin.VirtualDesktopManager", "string:desktops")
    return re.findall(r'string "([0-9a-f]{8}-[0-9a-f-]{27})"', out)


def set_desktop_count(n: int):
    for _ in range(10):
        ids = desktop_ids()
        if len(ids) == n:
            return ids
        if len(ids) < n:
            dbus(*VDM, "org.kde.KWin.VirtualDesktopManager.createDesktop", f"uint32:{len(ids)}", f"string:Desktop {len(ids) + 1}")
        else:
            dbus(*VDM, "org.kde.KWin.VirtualDesktopManager.removeDesktop", f"string:{ids[-1]}")
        time.sleep(0.5)
    return desktop_ids()


def item_of(rep: dict, label_part: str):
    items = (rep.get("report") or {}).get("items") or rep.get("items") or []
    return next((it for it in items if label_part in json.dumps(it)), items[0] if len(items) == 1 else None)


def run_all(e: Env):
    e.start_daemon()
    load_script()
    st = e.wait_for(lambda: (lambda s: s if s["window_control"] else None)(e.status()), 15.0) or e.status()
    check("W00", "KWin script attached; window control available", st["window_control"] and st["adapter"] == "kwin")

    ids = set_desktop_count(3)
    check("W01", "KWin has three workspaces", len(ids) == 3, f"{len(ids)} desktops")

    e.window("qt", "io.fractile.WsTest", "fractile-ws-test")
    w = e.wait_for(lambda: next((x["id"] for x in e.windows(True) if x.get("title") == "fractile-ws-test"), None), 15.0)
    check("W02", "test window appears", bool(w))
    if not w:
        return
    e.call("move_to_workspace", {"window": w, "workspace": 2})
    on3 = e.wait_for(lambda: next((x.get("workspace") for x in e.windows(True) if x["id"] == w), None) == 2, 3.0)
    e.cli_json("place", "--grid", "2x2", "--cells", "1,1,1,1", "--window", w)
    want = e.frame(w)
    e.cli_json("save", "--name", "ws3")
    check("W03", "window saved on workspace 3 in the bottom-right cell", bool(on3), f"frame {want}")

    # workspace 3 goes away; KWin moves the window to a remaining workspace
    ids = set_desktop_count(2)
    e.cli_json("snap", "top-left-quarter", "--window", w)
    ws_now = next((x.get("workspace") for x in e.windows(True) if x["id"] == w), None)
    check("W04", "workspace 3 removed; the window is now elsewhere", len(ids) == 2 and ws_now in (0, 1), f"{len(ids)} desktops, window on index {ws_now}")

    # a run with a failed item exits non-zero (1: failed, 6: partial); the
    # report is printed either way
    pr = e.run("--json", "restore", "--snapshot", "ws3")
    rep = json.loads(pr.stdout)
    it = item_of(rep, "fractile-ws-test")
    status = (it or {}).get("status")
    msg = (it or {}).get("message") or ""
    geom_ok = e.frame(w) == want
    check("W05", "the restore does not report the window as placed when KWin refused workspace 3, and says why",
          status not in SUCCESS and "workspace 3" in msg,
          f"status {status}; message {msg!r}; geometry restored {geom_ok}; run state {rep.get('state')}, exit {pr.returncode}")

    # workspace 3 is back: retrying the same run finishes the job
    ids = set_desktop_count(3)
    p = e.run("--json", "retry", rep["run_id"])
    rep2 = json.loads(p.stdout) if p.returncode == 0 else {}
    it2 = item_of(rep2, "fractile-ws-test")
    ws_after = e.wait_for(lambda: next((x.get("workspace") for x in e.windows(True) if x["id"] == w), None) == 2, 3.0)
    check("W06", "after workspace 3 is created again, retrying the run moves the window there and reports it placed",
          (it2 or {}).get("status") in SUCCESS and bool(ws_after) and e.frame(w) == want,
          f"retry rc {p.returncode} {p.stderr.strip()[:160]}; status {(it2 or {}).get('status')}; message {(it2 or {}).get('message')!r}; on workspace 3 {bool(ws_after)}; frame {e.frame(w)}")


def main():
    e = Env()
    try:
        run_all(e)
    except Exception as ex:
        check("WXX", "suite crashed", False, repr(ex))
    finally:
        for pr in e.procs:
            pr.terminate()
        e.stop_daemon()
    kv = subprocess.run(["kwin_wayland", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "e2e-kwin-wayland-ws-refused", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"{kv} --virtual (Wayland, no Xwayland) in an archlinux container (render device: {os.environ.get('FRACTILE_TEST_GPU', 'none')}); Fractile KWin script",
          "results": results}
    out = Path(os.environ.get("EVIDENCE", "/tmp/e2e-kwin-ws.json"))
    out.write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    if failed and (e.base / "daemon.out").exists():
        print("== daemon log (warnings)")
        print("\n".join([ln for ln in (e.base / "daemon.out").read_text().splitlines() if " Warn " in ln or " Error " in ln][-25:]))
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
