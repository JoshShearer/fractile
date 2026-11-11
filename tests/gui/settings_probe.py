#!/usr/bin/env python3
"""In-process driver for the real Settings window (run by settings_x11.py).

Runs the real FractileApp, connected to the live service over the real socket,
on the nested X display. Opens Settings the way `fractile settings` does, and
types into the shortcut editor with real key events sent by this test harness,
not by Fractile: xdotool on X11, Mutter's RemoteDesktop virtual keyboard on
GNOME Wayland (tests/tools/wlkeys.py). The editor holds an X keyboard grab while it
listens, so the keys reach it through the X server like a user's keys would.

Prints one JSON object: {"results": [...], "shots": [...]}.
Usage: settings_probe.py SHOT_DIR
"""
from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QCheckBox, QPushButton  # noqa: E402

from fractile_ui.app import FractileApp  # noqa: E402
from fractile_ui.settings import ShortcutDialog  # noqa: E402

results: list[dict] = []
shots: list[str] = []


def check(cid: str, name: str, ok: bool, detail: str = "") -> bool:
    results.append({"id": cid, "name": name, "ok": bool(ok), "detail": detail})
    return ok


def pump(sec: float) -> None:
    end = time.time() + sec
    while time.time() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def wait(fn, sec: float = 5.0):
    end = time.time() + sec
    while time.time() < end:
        v = fn()
        if v:
            return v
        pump(0.05)
    return fn()


WAYLAND = os.environ.get("XDG_SESSION_TYPE") == "wayland"
INJ: subprocess.Popen | None = None


def _inj_wait(word: str, sec: float) -> bool:
    """Wait for a line from the injector while the Qt loop keeps running."""
    buf = b""
    end = time.time() + sec
    while time.time() < end:
        if select.select([INJ.stdout], [], [], 0)[0]:
            chunk = os.read(INJ.stdout.fileno(), 256)
            if not chunk:
                return False
            buf += chunk
            if word.encode() + b"\n" in buf:
                return True
        pump(0.02)
    return False


def start_injector() -> None:
    """Wayland: one virtual keyboard for the whole run (Mutter RemoteDesktop
    on the headless shell's private bus). Creating it moves keyboard focus, so
    it is started before any window is focused."""
    global INJ
    if WAYLAND and INJ is None:
        INJ = subprocess.Popen([sys.executable, str(ROOT / "tests" / "tools" / "wlkeys.py"), "--stdin"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        _inj_wait("ready", 10.0)


def key(combo: str) -> None:
    if WAYLAND:
        # The Qt loop must keep dispatching while the keys go in; a blocking
        # wait here lost the events.
        start_injector()
        INJ.stdin.write((combo.lower() + "\n").encode())
        INJ.stdin.flush()
        _inj_wait("ok", 15.0)
    else:
        subprocess.run(["xdotool", "key", "--clearmodifiers", combo], timeout=10, check=False)


def harness_focus(app, title: str) -> None:
    """Headless Mutter does not focus a new window without user input. On
    Wayland the harness activates it through the service, as a user's click
    would. (On X11 the window manager focuses new windows.)"""
    if not WAYLAND:
        return
    win = wait(lambda: next((x for x in (app.bridge.try_call("list_windows", {"all": True}) or []) if x.get("title") == title), None), 5.0)
    if win:
        app.bridge.try_call("activate_window", {"window": win["id"]})
        pump(0.5)


def button(parent, text: str) -> QPushButton | None:
    return next((b for b in parent.findChildren(QPushButton) if b.text() == text), None)


def capture(edit, combo: str) -> str:
    """Click the shortcut button, press the keys, return the editor's message."""
    if not edit.capturing:
        edit.btn.click()
    wait(lambda: edit.capturing and QApplication.instance().focusWindow() is not None, 3.0)
    pump(0.3)
    prev = edit.info.text()
    key(combo)
    # wait for the editor's answer to this combination (the label still shows
    # the previous one until then)
    wait(lambda: edit.info.text() != prev and not edit.info.text().startswith("Could not"), 5.0)
    pump(0.5)
    return edit.info.text()


def drive_dialog(state: dict) -> None:
    d = QApplication.activeModalWidget()
    if not isinstance(d, ShortcutDialog):
        state["dialog"] = None
        state["modal"] = type(d).__name__ if d is not None else None
        if d is not None:
            d.reject()
        return
    state["dialog"] = True
    harness_focus(state["app"], d.windowTitle())
    ed = d.keys
    msg = capture(ed, "g")
    check("S03", "a global shortcut without a modifier is refused and the editor keeps listening",
          "need at least one modifier" in msg and ed.capturing and ed.value is None, msg)
    paused = (state["app"].bridge.try_call("status") or {}).get("hotkeys_paused")
    check("S14", "while the editor listens, global shortcuts are paused so the desktop passes their keys to it",
          paused is True and ed.capturing, f"hotkeys_paused={paused}")
    msg = capture(ed, "ctrl+alt+g")
    check("S04", "a combination Fractile already uses is reported by name (the panel shortcut)",
          msg.startswith("⚠") and "Show/hide panel" in msg and ed.value == "Ctrl+Alt+G", msg)
    if WAYLAND:
        # Ctrl+Alt+Delete is GNOME's log-out shortcut (media-keys) on every
        # GNOME version and distribution (Ctrl+Alt+T for the terminal is an
        # Ubuntu addition). The service reads the desktop's bindings.
        msg = capture(ed, "ctrl+alt+delete")
        check("S05", "a combination GNOME's settings assign is reported as taken, with where it is set",
              msg.startswith("⚠") and "assigned in desktop settings" in msg and "logout" in msg and ed.value.startswith("Ctrl+Alt+Del"), f"{msg} (value {ed.value})")
    else:
        msg = capture(ed, "ctrl+alt+shift+k")
        check("S05", "a combination another X client holds is reported as taken, in plain words (live grab test by the service)",
              msg.startswith("⚠") and "another application already uses" in msg and ed.value == "Ctrl+Alt+Shift+K", msg)
    msg = capture(ed, "ctrl+alt+shift+F9")
    check("S06", "a free combination is reported as available", msg.startswith("✓") and ed.value == "Ctrl+Alt+Shift+F9", msg)
    # Panel scope: navigation keys are reserved.
    d.scope.setCurrentIndex(d.scope.findData("local"))
    pump(0.2)
    msg = capture(ed, "Left")
    check("S07", "a panel shortcut on a navigation key is refused (reserved by the panel)",
          "panel uses this key" in msg and ed.value == "Ctrl+Alt+Shift+F9", msg)
    key("Escape")
    wait(lambda: not ed.capturing, 3.0)
    d.scope.setCurrentIndex(d.scope.findData("global"))
    pump(0.3)
    d.name.setText("Probe left half")
    d.grab().save(state["shot_dir"] + "/14-shortcut-editor.png")
    shots.append("14-shortcut-editor.png")
    ok = next((b for b in d.findChildren(QPushButton) if b.text().replace("&", "") == "OK"), None)
    if ok is None:
        d.reject()
        return
    ok.click()


def run(app: FractileApp, shot_dir: str) -> None:
    start_injector()
    w = wait(lambda: app.settings_win if app.settings_win and app.settings_win.isVisible() else None, 10.0)
    if not check("S01", "Settings opens from the service request with all eight pages", w is not None and w.tabs.count() == 8,
                 str([w.tabs.tabText(i) for i in range(w.tabs.count())]) if w else
                 f"no window (connected={app.bridge.calls is not None}, settings_win={app.settings_win is not None})"):
        return
    pump(0.5)
    harness_focus(app, w.windowTitle())
    for i in range(w.tabs.count()):
        w.tabs.setCurrentIndex(i)
        pump(0.4)
        name = f"{20 + i:02d}-settings-{w.PAGES[i]}.png"
        w.grab().save(f"{shot_dir}/{name}")
        shots.append(name)
    check("S02", "every page renders (screenshots in tests/evidence/gui/)", len(shots) == 8)

    # General page: a checkbox edit is held until Apply, then saved by the service.
    w.open_page("general")
    pump(0.3)
    cb = next((c for c in w.findChildren(QCheckBox) if c.text().startswith("Show the key hints")), None)
    before = cb.isChecked() if cb else None
    if cb:
        cb.click()
    pump(0.3)
    dirty = w.dirty_lbl.text() == "Unsaved changes" and w.apply_b.isEnabled()
    svc_before = (app.bridge.try_call("get_config") or {}).get("config", {}).get("general", {}).get("show_help")
    w.apply_b.click()
    pump(1.0)
    svc_after = (app.bridge.try_call("get_config") or {}).get("config", {}).get("general", {}).get("show_help")
    check("S08", "a General change is only pending until Apply, then the service has it",
          cb is not None and dirty and svc_before == before and svc_after == (not before) and not w.apply_b.isEnabled(),
          f"before={before} pending_dirty={dirty} service_before={svc_before} service_after={svc_after}")

    # Shortcuts page: add a shortcut through the real dialog.
    w.open_page("shortcuts")
    pump(0.3)
    add = button(w.tabs.currentWidget(), "Add…")  # the Rules page has one too
    state = {"shot_dir": shot_dir, "app": app}
    n0 = len(w.cfg.get("shortcuts", []))
    QTimer.singleShot(400, lambda: drive_dialog(state))
    if add:
        add.click()  # exec() runs drive_dialog inside the dialog's loop
    pump(0.5)
    added = [s for s in w.cfg.get("shortcuts", []) if s.get("name") == "Probe left half"]
    check("S09", "the new shortcut appears in the table, pending until Apply",
          state.get("dialog") and len(w.cfg["shortcuts"]) == n0 + 1 and added and w.apply_b.isEnabled(),
          f"dialog={state.get('dialog')} modal={state.get('modal')} add_button={add is not None} rows={w.sc_table.rowCount()}")
    after = (app.bridge.try_call("status") or {}).get("hotkeys_paused")
    check("S15", "the pause ends when the editor closes", after is False, f"hotkeys_paused={after}")
    w.apply_b.click()
    pump(1.5)
    w.grab().save(f"{shot_dir}/15-shortcuts-applied.png")
    shots.append("15-shortcuts-applied.png")
    svc = [s for s in (app.bridge.try_call("get_config") or {}).get("config", {}).get("shortcuts", []) if s.get("name") == "Probe left half"]
    errs = (app.bridge.try_call("status") or {}).get("hotkey_errors") or {}
    row = next((r for r in range(w.sc_table.rowCount()) if w.sc_table.item(r, 0).text() == "Probe left half"), None)
    status = w.sc_table.item(row, 4).text() if row is not None else None
    check("S10", "after Apply the service saved and registered it; the table says active",
          len(svc) == 1 and svc[0].get("accelerator") == "Ctrl+Alt+Shift+F9" and svc[0]["id"] not in errs and status == "active",
          f"service={svc} status={status}")
    # The panel shortcut (set under General) is listed with its real keys,
    # and Edit… on that row opens General.
    w.open_page("shortcuts")
    pump(0.3)
    prow = next((r for r in range(w.sc_table.rowCount()) if w.sc_table.item(r, 0).text() == "Show the panel"), None)
    pkeys = w.sc_table.item(prow, 1).text() if prow is not None else None
    pstat = w.sc_table.item(prow, 4).text() if prow is not None else None
    if prow is not None:
        w.sc_table.selectRow(prow)
        edit_b = button(w.tabs.currentWidget(), "Edit…")
        if edit_b:
            edit_b.click()
        pump(0.3)
    page = w.PAGES[w.tabs.currentIndex()]
    check("S13", "the shortcut list shows the panel shortcut with its real keys; Edit… on it opens General",
          pkeys == "Ctrl+Alt+G" and pstat == "active" and page == "general", f"keys={pkeys} status={pstat} page after Edit={page}")
    w.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("shot_dir")
    a = ap.parse_args()
    qapp = QApplication(sys.argv[:1])
    qapp.setQuitOnLastWindowClosed(False)
    args = argparse.Namespace(settings=True, page="shortcuts", launcher=False, dashboard=False, panel=False, palette=False)
    app = FractileApp(qapp, args)
    app.start()

    def go():
        try:
            run(app, a.shot_dir)
        except Exception as ex:  # noqa: BLE001
            check("SXX", "probe crashed", False, repr(ex))
        qapp.quit()

    QTimer.singleShot(0, go)
    qapp.exec()
    if INJ is not None:
        INJ.stdin.close()
        INJ.wait(10)
    print(json.dumps({"results": results, "shots": shots}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
