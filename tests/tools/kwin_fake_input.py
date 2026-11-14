#!/usr/bin/env python3
"""Test-only pointer for a headless KWin (kwin_wayland --virtual in the test
container): KWin's org_kde_kwin_fake_input protocol through pywayland.
Fractile itself never injects input.

KWin offers fake input only to clients whose desktop file lists it in
X-KDE-Wayland-Interfaces; the container suite writes such a file for the
Python interpreter (tests/kwin/e2e_kwin_two.py).

Stdin protocol, one command per line, "ok" after each:
  abs X Y          move the pointer to global logical coordinates
  button down|up   left button
Prints "ready" once bound.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
from pathlib import Path

XML = "/usr/share/plasma-wayland-protocols/fake-input.xml"
BTN_LEFT = 0x110


def protocol_module():
    out = Path(tempfile.mkdtemp(prefix="fractile-fake-input-"))
    subprocess.run([sys.executable, "-m", "pywayland.scanner", "-i", "/usr/share/wayland/wayland.xml", XML, "-o", str(out)],
                   check=True, capture_output=True)
    sys.path.insert(0, str(out))
    return importlib.import_module("fake_input")


def main() -> int:
    from pywayland.client import Display
    fi_mod = protocol_module()
    FakeInput = fi_mod.OrgKdeKwinFakeInput
    display = Display()
    display.connect()
    found = {}

    def on_global(registry, name, interface, version):
        if interface == "org_kde_kwin_fake_input":
            found["fi"] = registry.bind(name, FakeInput, min(version, 4))

    registry = display.get_registry()
    registry.dispatcher["global"] = on_global
    display.dispatch(block=True)
    display.roundtrip()
    fi = found.get("fi")
    if fi is None:
        print("no org_kde_kwin_fake_input (is the interpreter allowed in a desktop file?)", file=sys.stderr, flush=True)
        return 2
    fi.authenticate("fractile-tests", "test harness pointer")
    display.roundtrip()
    print("ready", flush=True)
    for line in sys.stdin:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "abs":
            fi.pointer_motion_absolute(float(parts[1]), float(parts[2]))
        elif parts[0] == "button":
            fi.button(BTN_LEFT, 1 if parts[1] == "down" else 0)
        else:
            print(f"unknown command {parts[0]}", file=sys.stderr, flush=True)
            return 2
        display.roundtrip()
        print("ok", flush=True)
    display.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
