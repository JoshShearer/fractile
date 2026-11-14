#!/usr/bin/env python3
"""Reads one application's accessibility tree over AT-SPI, as a screen
reader does, and lists the controls it would announce without a name.

Usage: atspi_names.py PID WINDOW_TITLE
Prints JSON: {"found": bool, "controls": N, "unnamed": [{"role", "path"}...]}
Only what is showing counts (a hidden page or tab is not announced).
"""
from __future__ import annotations

import json
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

R = Atspi.Role
CONTROLS = {R.PUSH_BUTTON, R.TOGGLE_BUTTON, R.CHECK_BOX, R.RADIO_BUTTON, R.COMBO_BOX, R.TEXT, R.ENTRY, R.PASSWORD_TEXT,
            R.SPIN_BUTTON, R.SLIDER, R.LIST, R.LIST_BOX, R.TABLE, R.TREE, R.TREE_TABLE, R.PAGE_TAB, R.MENU_ITEM}
CUSTOM = {R.CANVAS, R.PANEL, R.FRAME, R.FILLER}  # a focusable custom widget (the grid) shows up as one of these


def walk(node, path, out, depth=0):
    if node is None or depth > 40:
        return
    try:
        st = node.get_state_set()
        role = node.get_role()
        name = node.get_name() or ""
    except Exception:  # noqa: BLE001  the object went away
        return
    if not st.contains(Atspi.StateType.SHOWING):
        return
    here = f"{path}/{node.get_role_name()}" + (f"[{name[:30]}]" if name else "")
    focusable = st.contains(Atspi.StateType.FOCUSABLE)
    if (role in CONTROLS or (role in CUSTOM and focusable and depth > 1)) and (focusable or role == R.PAGE_TAB):
        out["controls"] += 1
        if not name.strip():
            out["unnamed"].append({"role": node.get_role_name(), "path": here[-160:]})
    for i in range(node.get_child_count()):
        try:
            walk(node.get_child_at_index(i), here, out, depth + 1)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    pid, title = int(sys.argv[1]), sys.argv[2]
    desk = Atspi.get_desktop(0)
    out = {"found": False, "controls": 0, "unnamed": [], "apps": []}
    for i in range(desk.get_child_count()):
        app = desk.get_child_at_index(i)
        if app is None:
            continue
        try:
            apid = app.get_process_id()
        except Exception:  # noqa: BLE001
            continue
        out["apps"].append(f"{app.get_name()}:{apid}")
        if apid != pid:
            continue
        for j in range(app.get_child_count()):
            win = app.get_child_at_index(j)
            if win is not None and (win.get_name() or "") == title:
                out["found"] = True
                walk(win, win.get_name(), out)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
