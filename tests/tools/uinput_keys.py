#!/usr/bin/env python3
"""Test-only keyboard for a throwaway test VM: a uinput device (python3-evdev),
which the compositor picks up through libinput like a plugged-in keyboard.
Fractile itself never injects input. Needs write access to /dev/uinput
(run with sudo in the VM).

Same stdin protocol as wlkeys.py --stdin: prints "ready", then reads one key
combination per line (e.g. ctrl+alt+shift+F9) and prints "ok" after each.
"""
import sys
import time

from evdev import UInput, ecodes as e

NAMES = {"ctrl": "LEFTCTRL", "control": "LEFTCTRL", "alt": "LEFTALT", "shift": "LEFTSHIFT", "super": "LEFTMETA",
         "escape": "ESC", "esc": "ESC", "return": "ENTER", "enter": "ENTER", "delete": "DELETE", "del": "DELETE",
         "left": "LEFT", "right": "RIGHT", "up": "UP", "down": "DOWN", "equal": "EQUAL", "minus": "MINUS",
         "space": "SPACE", "tab": "TAB", "backspace": "BACKSPACE"}


def code(name: str) -> int:
    n = name.lower()
    k = NAMES.get(n) or n.upper()
    c = getattr(e, f"KEY_{k}", None)
    if c is None:
        raise SystemExit(f"unknown key {name}")
    return c


def main() -> int:
    keys = [v for k, v in vars(e).items() if k.startswith("KEY_") and isinstance(v, int) and v < 0x200]
    ui = UInput({e.EV_KEY: keys}, name="fractile-test-keyboard")
    time.sleep(1.0)  # libinput and the compositor add the new device
    print("ready", flush=True)
    for line in sys.stdin:
        combo = line.strip()
        if not combo:
            continue
        cs = [code(k) for k in combo.split("+")]
        for c in cs:
            ui.write(e.EV_KEY, c, 1)
            ui.syn()
            time.sleep(0.03)
        for c in reversed(cs):
            ui.write(e.EV_KEY, c, 0)
            ui.syn()
            time.sleep(0.03)
        time.sleep(0.15)
        print("ok", flush=True)
    ui.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
