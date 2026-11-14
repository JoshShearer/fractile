#!/usr/bin/env python3
"""Accessibility suite (UX-06) on a nested X11 desktop: GNOME Shell by
default, kwin_x11 with FRACTILE_TEST_WM=kwin (see nested-gnome-x11.sh).

Starts the service, a target window and an AT-SPI accessibility bus, then
runs tests/gui/a11y_probe.py (the real UI, in process) twice: in English
for A1-A3 and in an Arabic locale for A4 (right to left).

Containers: tests/gui/a11y-in-container.sh (Plasma X11) and
tests/gnome/x11-gnome-in-container.sh fedora:39 tests/gui/a11y_x11.py (GNOME).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "integration"))
sys.path.insert(0, str(ROOT / "tests" / "gui"))
from e2e_x11 import BIN, Env, check, results  # noqa: E402
from gui_x11 import environment  # noqa: E402


def session_bus(env: dict) -> tuple[str, int]:
    """A session bus with the system's service files, so the accessibility
    bus (org.a11y.Bus) is activated on demand as in a desktop session."""
    out = subprocess.run(["dbus-daemon", "--session", "--fork", "--print-address=1", "--print-pid=1"],
                         env=env, capture_output=True, text=True, check=True).stdout.split()
    return out[0], int(out[1])


def probe(env: dict, *args) -> list[dict]:
    p = subprocess.run([sys.executable, str(ROOT / "tests" / "gui" / "a11y_probe.py"), *args], env=env,
                       capture_output=True, text=True, timeout=600)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])["results"]
    except (ValueError, IndexError):
        return [{"id": "AXX", "name": f"probe {' '.join(args) or 'en'} produced results", "ok": False,
                 "detail": (p.stdout + p.stderr)[-800:]}]


def run_all(e: Env) -> None:
    e.start_daemon()
    e.run("config", "set", "general.first_run_complete", "true")
    e.run("config", "set", "general.tray_icon", "false")
    # One session bus for everything, started before any client: a client
    # without one (the GTK target) would autolaunch its own, with its own
    # accessibility bus, and the reader could end up looking at that one.
    addr, bus_pid = session_bus(e.env)
    e.env["DBUS_SESSION_BUS_ADDRESS"] = addr
    env = dict(e.env)
    try:
        e.window("gtk4", "org.fractile.A11y", "Target")
        if not check("A00", "target window is up", bool(e.wait_window(lambda w: w.get("app_id") == "org.fractile.A11y"))):
            return
        env.update(DBUS_SESSION_BUS_ADDRESS=addr, QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1", QT_QPA_PLATFORM="xcb",
                   PATH=f"{BIN}:{env.get('PATH', '/usr/bin:/bin')}")
        for r in probe(env):
            check(r["id"], r["name"], r["ok"], r.get("detail", ""))
        rtl = dict(env, LANG="ar_EG.UTF-8", LC_ALL="ar_EG.UTF-8", LANGUAGE="ar")
        for r in probe(rtl, "--rtl"):
            check(r["id"], r["name"], r["ok"], r.get("detail", ""))
    finally:
        try:
            os.kill(bus_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", type=int, default=27)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "a11y-x11.json"))
    a = ap.parse_args()
    nested = ROOT / "tests" / "integration" / "nested-gnome-x11.sh"
    st = subprocess.run(["bash", str(nested), "start", str(a.display), "1600x1200"], capture_output=True, text=True)
    if st.returncode != 0:
        print(st.stdout, st.stderr)
        return 2
    e = Env(a.display)
    try:
        run_all(e)
    except Exception as ex:  # noqa: BLE001
        check("AXX", "suite crashed", False, repr(ex))
    finally:
        e.cleanup()
        if not a.keep:
            subprocess.run(["bash", str(nested), "stop", str(a.display)])
    ev = {"suite": "a11y-x11", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": environment() + ", AT-SPI (at-spi2-core)", "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
