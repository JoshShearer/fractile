#!/usr/bin/env python3
"""Test-only key injector for a *nested/headless* GNOME Shell, via Mutter's
RemoteDesktop D-Bus API on that session's private bus. Fractile itself never
injects input; this exists so integration tests can press real shortcuts.

  wlkeys.py ctrl+alt+1 [more combos...]
  wlkeys.py --stdin      one combination per input line; prints "ok" after each
"""
import sys
import time

from gi.repository import Gio, GLib

KEYSYMS = {"ctrl": 0xFFE3, "control": 0xFFE3, "alt": 0xFFE9, "shift": 0xFFE1, "super": 0xFFEB,
           "escape": 0xFF1B, "return": 0xFF0D, "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
           "equal": 0x3D, "minus": 0x2D, "space": 0x20, "tab": 0xFF09}


def keysym(name: str) -> int:
    n = name.lower()
    if n in KEYSYMS:
        return KEYSYMS[n]
    if len(name) == 1:
        return ord(name.lower())
    if n.startswith("f") and n[1:].isdigit():
        return 0xFFBE + int(n[1:]) - 1
    raise SystemExit(f"unknown key {name}")


def main():
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    rd = Gio.DBusProxy.new_sync(bus, 0, None, "org.gnome.Mutter.RemoteDesktop", "/org/gnome/Mutter/RemoteDesktop", "org.gnome.Mutter.RemoteDesktop")
    path = rd.call_sync("CreateSession", None, 0, 5000).unpack()[0]
    s = Gio.DBusProxy.new_sync(bus, 0, None, "org.gnome.Mutter.RemoteDesktop", path, "org.gnome.Mutter.RemoteDesktop.Session")
    s.call_sync("Start", None, 0, 5000)
    # The virtual keyboard's first event can be consumed while the device
    # is being set up; send a harmless modifier tap first.
    time.sleep(0.3)
    s.call_sync("NotifyKeyboardKeysym", GLib.Variant("(ub)", (KEYSYMS["shift"], True)), 0, 5000)
    s.call_sync("NotifyKeyboardKeysym", GLib.Variant("(ub)", (KEYSYMS["shift"], False)), 0, 5000)
    time.sleep(0.3)
    def press(combo):
        keys = [keysym(k) for k in combo.split("+")]
        for k in keys:
            s.call_sync("NotifyKeyboardKeysym", GLib.Variant("(ub)", (k, True)), 0, 5000)
            time.sleep(0.02)
        for k in reversed(keys):
            s.call_sync("NotifyKeyboardKeysym", GLib.Variant("(ub)", (k, False)), 0, 5000)
            time.sleep(0.02)
        time.sleep(0.15)

    if sys.argv[1:] == ["--stdin"]:
        # One virtual keyboard for the whole run: creating and removing the
        # device for every combination makes Mutter drop keyboard focus.
        print("ready", flush=True)
        for line in sys.stdin:
            if line.strip():
                press(line.strip())
            print("ok", flush=True)
    else:
        for combo in sys.argv[1:]:
            press(combo)
    s.call_sync("Stop", None, 0, 5000)


main()
