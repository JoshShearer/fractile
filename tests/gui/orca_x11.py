#!/usr/bin/env python3
"""Screen-reader suite (UX-06): Orca runs against the real UI on a nested
GNOME X11 desktop, and the checks read what Orca says (its debug log's
speech output; speech-dispatcher has no audio device here, so nothing is
heard, but Orca still decides and logs every utterance).

The UI is started by the service as for a user, without forcing Qt's
accessibility on: Qt has to notice the running screen reader itself.

  O1  opening the panel announces the placement grid and how to use it
  O1b moving the selection with the arrow keys reads the columns and rows
  O2  moving through Settings with Tab announces a name for every stop
      (never a bare role such as "push button")
  O3  the command palette announces its search field and the command list

Run in the GNOME 46 image: tests/gnome46-x11/run.sh tests/gui/orca_x11.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "integration"))
sys.path.insert(0, str(ROOT / "tests" / "gui"))
from e2e_x11 import check, results  # noqa: E402
from gui_x11 import GuiEnv, environment  # noqa: E402

BARE_ROLES = {"push button", "button", "check box", "combo box", "text", "entry", "spin button", "list", "table",
              "page tab", "toggle button", "radio button", "slider", "panel", "filler", "layered pane", "canvas",
              "split pane", "tree", "menu item", "page tab list"}


class Orca:
    def __init__(self, env: dict, log: Path):
        self.log = log
        # line-buffered debug file, so the checks see speech as it happens
        self.p = subprocess.Popen([sys.executable, str(ROOT / "tests" / "tools" / "orca_linebuffered.py"), str(log), "--replace"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        self.mark = 0

    def text(self) -> str:
        try:
            return self.log.read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def spoken(self) -> list[str]:
        """Utterances since the last mark()."""
        t = self.text()[self.mark:]
        return [m.strip() for m in re.findall(r"SPEECH OUTPUT: '([^']*)'", t) if m.strip()]

    def mark_now(self) -> None:
        self.mark = len(self.text())

    def stop(self) -> None:
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(5)
            except subprocess.TimeoutExpired:
                self.p.kill()


def ui_pids() -> list[int]:
    out = subprocess.run(["pgrep", "-f", "fractile-ui"], capture_output=True, text=True).stdout.split()
    return [int(x) for x in out]


def atspi_apps(env: dict) -> list[str]:
    p = subprocess.run([sys.executable, str(ROOT / "tests" / "tools" / "atspi_names.py"), "0", "-"], env=env,
                       capture_output=True, text=True, timeout=30)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1]).get("apps", [])
    except (ValueError, IndexError):
        return [p.stderr[-200:]]


def session_bus(env: dict) -> tuple[str, int]:
    out = subprocess.run(["dbus-daemon", "--session", "--fork", "--print-address=1", "--print-pid=1"],
                         env=env, capture_output=True, text=True, check=True).stdout.split()
    return out[0], int(out[1])


def run_all(e: GuiEnv) -> None:
    addr, bus_pid = session_bus(e.env)
    e.env["DBUS_SESSION_BUS_ADDRESS"] = addr
    e.env.pop("QT_LINUX_ACCESSIBILITY_ALWAYS_ON", None)
    orca = Orca(e.env, e.base / "orca.log")
    try:
        ready = e.wait_for(lambda: len(orca.text()) > 2000, 30.0, 0.5)
        if not check("O00", "Orca is running and logging", bool(ready) and orca.p.poll() is None,
                     f"orca exit {orca.p.poll()}; log {len(orca.text())} bytes"):
            return
        time.sleep(3)
        e.start_daemon()
        e.run("config", "set", "general.first_run_complete", "true")
        e.run("config", "set", "general.tray_icon", "false")
        e.window("gtk4", "org.fractile.Editor", "Editor window")
        if not check("O00b", "target window is up", bool(e.wait_window(lambda w: w.get("app_id") == "org.fractile.Editor"))):
            return
        time.sleep(1)

        # O1: the panel
        orca.mark_now()
        e.run("panel", "show")
        said = e.wait_for(lambda: [s for s in orca.spoken() if "Placement grid" in s], 15.0, 0.3)
        allsaid = orca.spoken()
        diag = f"; ui pids {ui_pids()}; atspi apps {atspi_apps(e.env)}" if not said else ""
        check("O1", "opening the panel announces the placement grid and how to use it",
              any("Placement grid" in x and "Arrow keys" in x for x in allsaid), f"said: {allsaid[:12]}{diag}")
        # O1b: choosing cells with the keyboard is read out
        orca.mark_now()
        e.xdo("key", "Right")
        time.sleep(0.6)
        e.xdo("key", "shift+Right")
        e.xdo("key", "shift+Down")
        sel = e.wait_for(lambda: [x for x in orca.spoken() if x.startswith("Placement grid: columns") and "rows" in x], 8.0, 0.3)
        qt_bug = "Bad content sent to speak()" in orca.text()[orca.mark:]
        check("O1b", "moving the selection with the keyboard reads the chosen columns and rows",
              bool(sel), f"said: {orca.spoken()[:8]}" + ("; Orca dropped the grid's name changes: this Qt sends the AT-SPI "
                                                         "accessible-name change without the new name (seen with Qt 6.4; Qt 6.10 sends it)" if qt_bug and not sel else ""))
        e.xdo("key", "Escape")
        time.sleep(1)

        # O2: Tab through Settings
        e.run("settings")
        e.wait_for(lambda: e.visible_named("Fractile Settings"), 15.0, 0.3)
        time.sleep(2)
        orca.mark_now()
        for _ in range(20):
            e.xdo("key", "Tab")
            time.sleep(0.5)
        time.sleep(3)  # speech trails the keys
        t = orca.text()[orca.mark:]
        focused = []
        for role, name in re.findall(r"EVENT MANAGER: object:state-changed:focused for \[([^:\]]+): '([^']*)'\] in (?:\[application: 'Fractile'\]|Fractile \()", t):
            if (role, name) not in focused:
                focused.append((role, name))
        unnamed = [r for r, n in focused if not n.strip()]
        bare = [u for u in orca.spoken() if u.rstrip(".").strip().lower() in BARE_ROLES]
        rec = [n for r, n in focused if n.startswith("Show/hide panel shortcut:")]
        check("O2", "Tab through Settings: every control that takes focus is announced by name (no bare role), "
              "and the shortcut recorder says what it sets and its keys",
              len(focused) >= 8 and not unnamed and not bare and bool(rec),
              f"{len(focused)} controls focused; unnamed {unnamed}; bare roles spoken {bare}; recorder {rec}; "
              f"focused: {[f'{r}: {n}' for r, n in focused][:12]}")
        e.xdo("key", "Escape")
        time.sleep(1)

        # O3: the command palette
        orca.mark_now()
        e.run("palette")
        said = e.wait_for(lambda: [s for s in orca.spoken() if "Command" in s], 15.0, 0.3)
        orca.mark_now()
        e.xdo("key", "Down")
        down = e.wait_for(lambda: [d for d in orca.spoken() if d.startswith("Snap")], 8.0, 0.3) or orca.spoken()
        check("O3", "the command palette announces its search field, and moving down reads a command",
              bool(said) and any(d.startswith("Snap") or "half" in d for d in down), f"open: {said}; down: {down[:4]}")
    finally:
        orca.stop()
        keep = os.environ.get("FRACTILE_ORCA_LOG")
        if keep:
            Path(keep).write_text(orca.text())
        try:
            os.kill(bus_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", type=int, default=29)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "orca-x11.json"))
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
        check("OXX", "suite crashed", False, repr(ex))
    finally:
        e.cleanup()
        if not a.keep:
            subprocess.run(["bash", str(nested), "stop", str(a.display)])
    ov = subprocess.run(["orca", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "orca-x11", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"{environment()}, Orca {ov} (speech logged, no audio)", "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
