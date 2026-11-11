#!/usr/bin/env python3
"""Tray icon (DV-19) on GNOME Shell Wayland with the AppIndicator extension.

Stock GNOME has no tray. Most GNOME users get one from the AppIndicator
extension (Ubuntu ships it enabled), which hosts StatusNotifierItems, the
protocol KDE Plasma uses too. This suite loads that extension into a private
headless gnome-shell (fetched without root by tests/tools/fetch-appindicator.sh)
next to the Fractile extension, and talks to the icon the way the top bar
does: StatusNotifierWatcher, the item's properties, its dbusmenu, Activate.
A test-only helper extension (tests/tools/unsafe-mode@fractile.test) lets the
harness read the top bar and screenshot it. A second shell without any tray
checks that the background UI exits.

Usage: tests/gui/tray_gnome_wayland.py [--keep] [--evidence FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from gi.repository import Gio, GLib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "integration"))
import e2e_gnome_wayland as gw  # noqa: E402
from e2e_gnome_wayland import EXT_UUID, Env, check, results  # noqa: E402

SHOTS = Path(os.environ.get("FRACTILE_SHOT_DIR") or ROOT / "tests" / "evidence" / "gui" / "wayland")
UNSAFE = ROOT / "tests" / "tools" / "unsafe-mode@fractile.test"
WATCHER = ("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher")


class Bus:
    def __init__(self, address: str):
        self.c = Gio.DBusConnection.new_for_address_sync(
            address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)

    def call(self, dest, path, iface, method, args=None, sig=None, timeout=5000):
        v = self.c.call_sync(dest, path, iface, method, args, GLib.VariantType(sig) if sig else None, 0, timeout, None)
        return v.unpack() if v is not None else None

    def prop(self, dest, path, iface, name):
        return self.call(dest, path, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (iface, name)))[0]

    def has_owner(self, name):
        return self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner", GLib.Variant("(s)", (name,)))[0]

    def pid_of(self, name):
        return self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetConnectionUnixProcessID", GLib.Variant("(s)", (name,)))[0]

    def eval(self, js):
        ok, out = self.call("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell", "Eval", GLib.Variant("(s)", (js,)))
        if not ok or not out:
            return None
        v = json.loads(out)
        return json.loads(v) if isinstance(v, str) else v  # the JS side returns JSON text


def items(bus: Bus) -> list[str]:
    try:
        return list(bus.prop(*WATCHER, "RegisteredStatusNotifierItems"))
    except GLib.Error:
        return []


def split_item(it: str) -> tuple[str, str]:
    # "<bus name>@<object path>" (this watcher), "<bus name><object path>"
    # or "<bus name>" (path /StatusNotifierItem)
    i = it.find("/")
    if i < 0:
        return it, "/StatusNotifierItem"
    return it[:i].rstrip("@"), it[i:]


def menu_labels(bus: Bus, dest: str, path: str) -> dict[str, int]:
    _rev, layout = bus.call(dest, path, "com.canonical.dbusmenu", "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
    out: dict[str, int] = {}

    def walk(node):
        nid, props, children = node
        label = props.get("label")
        if label and props.get("visible", True):
            out[label.replace("_", "")] = nid
        for ch in children:
            walk(ch)
    walk(layout)
    return out


def ours(bus: Bus, e: Env):
    """Fractile's item: registered by a running fractile-ui process."""
    uis = set(e.ui_pids())
    for it in items(bus):
        dest, path = split_item(it)
        try:
            if bus.pid_of(dest) in uis:
                return dest, path
        except GLib.Error:
            pass
    return None


def with_tray(keep: bool) -> None:
    if os.environ.get("FRACTILE_APPINDICATOR_DIR"):  # e.g. the distribution's package in a container
        appind = Path(os.environ["FRACTILE_APPINDICATOR_DIR"])
    else:
        fetch = subprocess.run(["bash", str(ROOT / "tests" / "tools" / "fetch-appindicator.sh")], capture_output=True, text=True)
        if fetch.returncode != 0:
            check("Y00", "AppIndicator extension available for the test", False, fetch.stderr[-300:])
            return
        appind = Path(fetch.stdout.strip().splitlines()[-1])
    e = Env(extra_exts=[appind, UNSAFE])
    try:
        Path(e.env["FRACTILE_CONFIG"]).write_text("[general]\nfirst_run_complete = true\npanel_shortcut = \"Ctrl+Alt+G\"\n")
        bus = Bus(e.env["DBUS_SESSION_BUS_ADDRESS"])
        host = e.wait_for(lambda: bus.has_owner(WATCHER[0]) and bus.prop(*WATCHER, "IsStatusNotifierHostRegistered"), 10.0)
        meta = json.loads((appind / "metadata.json").read_text())
        check("Y00", "the AppIndicator extension runs in the test shell and hosts StatusNotifierItems",
              bool(host), f"{meta['uuid']} shell-version {meta['shell-version']}")
        t0 = time.time()
        e.start_daemon()
        e.wait_for(lambda: e.status()["window_control"], 10.0)
        it = e.wait_for(lambda: ours(bus, e), 20.0, 0.2)
        took = time.time() - t0
        wins = [w for w in e.windows(True) if w.get("is_own")]
        if not check("Y01", "with the tray icon on, starting the service puts Fractile's icon in the tray, with no window opened",
                     bool(it) and not wins, f"item={it} after {took:.1f} s; own windows={[w['title'] for w in wins]}"):
            return
        dest, path = it
        sni = "org.kde.StatusNotifierItem"
        props = {k: bus.prop(dest, path, sni, k) for k in ("Id", "Title", "Status", "Category")}
        tip = bus.prop(dest, path, sni, "ToolTip")
        pix = bus.prop(dest, path, sni, "IconPixmap")
        icon_name = bus.prop(dest, path, sni, "IconName")
        check("Y02", "the icon describes itself: name, status Active, an icon image and a tooltip",
              props["Status"] == "Active" and (bool(pix) or bool(icon_name)) and "Fractile" in json.dumps(tip),
              f"{props} icon_name={icon_name!r} pixmaps={[(w, h) for w, h, _ in pix]} tooltip={tip[2]!r}")
        # The top bar shows it.
        shown = e.wait_for(lambda: bus.eval(
            "JSON.stringify(Object.entries(Main.panel.statusArea).filter(([k]) => k.startsWith('appindicator'))"
            ".map(([k, a]) => ({key: k, visible: a.visible && a.get_parent() !== null && a.mapped, w: a.width, h: a.height})))"), 10.0)
        vis = [a for a in (shown or []) if a["visible"] and a["w"] > 0]
        shot = SHOTS / "30-tray-topbar.png"
        SHOTS.mkdir(parents=True, exist_ok=True)
        try:
            bus.call("org.gnome.Shell.Screenshot", "/org/gnome/Shell/Screenshot", "org.gnome.Shell.Screenshot", "ScreenshotArea",
                     GLib.Variant("(iiiibs)", (1100, 0, 500, 32, False, str(shot))))
        except GLib.Error as ex:
            print("screenshot failed:", ex)
        check("Y03", "the GNOME top bar shows the icon (screenshot 30-tray-topbar.png)",
              len(vis) == 1, f"top bar indicators: {shown}")
        labels = menu_labels(bus, dest, bus.prop(dest, path, sni, "Menu"))
        want = ["Show panel", "Command palette", "Save workspace now", "Restore latest…", "Workspace dashboard", "Settings", "Quit Fractile"]
        check("Y04", "the icon's menu (dbusmenu, as the top bar reads it) has the Fractile actions",
              all(w in labels for w in want), str(list(labels)))
        # Choosing Settings in the menu opens Settings.
        mpath = bus.prop(dest, path, sni, "Menu")
        bus.call(dest, mpath, "com.canonical.dbusmenu", "Event",
                 GLib.Variant("(isvu)", (labels["Settings"], "clicked", GLib.Variant("i", 0), 0)))
        st = e.wait_for(lambda: next((w for w in e.windows(True) if w.get("title") == "Fractile Settings"), None), 8.0)
        check("Y05", "choosing Settings in the tray menu opens Settings", bool(st), str(st and st["id"]))
        # Save workspace now: a snapshot appears.
        n0 = len(e.cli_json("list-snapshots"))
        bus.call(dest, mpath, "com.canonical.dbusmenu", "Event",
                 GLib.Variant("(isvu)", (labels["Save workspace now"], "clicked", GLib.Variant("i", 0), 0)))
        n1 = e.wait_for(lambda: (lambda n: n if n > n0 else None)(len(e.cli_json("list-snapshots"))), 8.0)
        check("Y06", "'Save workspace now' in the tray menu saves a snapshot", bool(n1), f"snapshots {n0} -> {n1}")
        # Left click (Activate) shows the panel.
        bus.call(dest, path, sni, "Activate", GLib.Variant("(ii)", (800, 10)))
        pv = e.wait_for(lambda: next((w for w in e.windows(True) if (w.get("title") or "").startswith("Fractile panel")), None), 8.0)
        check("Y07", "clicking the icon shows the placement panel", bool(pv), str(pv and pv["title"]))
        # Turning the icon off removes it; on again brings it back.
        e.run("config", "set", "general.tray_icon", "false")
        gone = e.wait_for(lambda: ours(bus, e) is None, 8.0)
        e.run("config", "set", "general.tray_icon", "true")
        back = e.wait_for(lambda: ours(bus, e), 8.0)
        check("Y08", "turning the tray icon off in settings removes it; on brings it back", bool(gone) and bool(back),
              f"removed={bool(gone)} back={bool(back)}")
        check("Y09", "the UI logged no Python errors", "Traceback" not in (e.ui_log.read_text() if e.ui_log.exists() else ""),
              (e.ui_log.read_text()[-300:] if e.ui_log.exists() else ""))
    finally:
        e.cleanup(keep)


def without_tray(keep: bool) -> None:
    gw.NAME = gw.NAME + "-notray"
    e = Env()
    try:
        Path(e.env["FRACTILE_CONFIG"]).write_text("[general]\nfirst_run_complete = true\n")
        e.start_daemon()
        e.wait_for(lambda: e.status()["window_control"], 10.0)
        started = e.wait_for(lambda: e.ui_pids(), 10.0)
        wins = [w for w in e.windows(True) if w.get("is_own")]
        gone = e.wait_for(lambda: not e.ui_pids(), 45.0, 0.5)
        log = e.ui_log.read_text() if e.ui_log.exists() else ""
        check("Y10", "without a tray on the desktop, the background UI opens no window and exits (about 30 s)",
              bool(started) and gone and not wins and "no tray icon to show" in log,
              f"started={started} exited={gone} windows={[w['title'] for w in wins]} log={log.strip()[-160:]!r}")
        # The panel still works afterwards (the service starts the UI on demand).
        e.run("panel", "show")
        pv = e.wait_for(lambda: next((w for w in e.windows(True) if (w.get("title") or "").startswith("Fractile panel")), None), 10.0)
        check("Y11", "after that UI exited, the panel still opens on request", bool(pv), str(pv and pv["title"]))
    finally:
        e.cleanup(keep)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "tray-gnome-wayland.json"))
    a = ap.parse_args()
    for part in (with_tray, without_tray):
        try:
            part(a.keep)
        except Exception as ex:  # noqa: BLE001
            check("YXX", f"{part.__name__} crashed", False, repr(ex))
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "tray-gnome-wayland", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"headless {gs} --wayland --no-x11, Fractile extension {EXT_UUID}, Ubuntu AppIndicators extension (gnome-shell-extension-appindicator 58), PyQt6 UI",
          "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
