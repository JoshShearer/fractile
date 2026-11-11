#!/usr/bin/env python3
"""Test client window with controllable identity, used by integration tests
as a stand-in "application". Toolkits: gtk4 (client-side decorations), qt
(server-side decorations). Never used in production.

  testwin.py --toolkit gtk4 --app-id io.fractile.TestA --title "Doc A" [--windows 2]
"""
import argparse
import os
import sys

p = argparse.ArgumentParser()
p.add_argument("--toolkit", default="gtk4", choices=["gtk4", "qt"])
p.add_argument("--app-id", default="io.fractile.TestApp")
p.add_argument("--title", default="Fractile Test Window")
p.add_argument("--role", default=None)
p.add_argument("--windows", type=int, default=1)
p.add_argument("--size", default="640x420")
p.add_argument("--min-size", default=None, help="WxH minimum size hint")
p.add_argument("--fight", action="store_true", help="re-apply own size 300ms after map (app overwrites geometry)")
a = p.parse_args()
W, H = (int(v) for v in a.size.split("x"))

if a.toolkit == "gtk4":
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, GLib
    GLib.set_prgname(a.app_id)
    app = Gtk.Application(application_id=a.app_id)

    def on_activate(app):
        for i in range(a.windows):
            w = Gtk.ApplicationWindow(application=app)
            w.set_title(a.title if a.windows == 1 else f"{a.title} {i + 1}")
            w.set_default_size(W, H)
            w.set_child(Gtk.Label(label=f"{a.app_id} #{i + 1}\npid {os.getpid()}"))
            if a.min_size:
                mw, mh = (int(v) for v in a.min_size.split("x"))
                w.get_child().set_size_request(mw, mh)
            w.present()

    app.connect("activate", on_activate)
    sys.exit(app.run([]))
else:
    from PyQt6.QtWidgets import QApplication, QLabel
    from PyQt6.QtCore import QTimer
    app = QApplication([sys.argv[0], "-name", a.app_id.split(".")[-1].lower()])
    app.setApplicationName(a.app_id.split(".")[-1].lower())
    app.setDesktopFileName(a.app_id)
    wins = []
    for i in range(a.windows):
        l = QLabel(f"{a.app_id} #{i + 1}\npid {os.getpid()}")
        l.setWindowTitle(a.title if a.windows == 1 else f"{a.title} {i + 1}")
        l.resize(W, H)
        if a.min_size:
            mw, mh = (int(v) for v in a.min_size.split("x"))
            l.setMinimumSize(mw, mh)
        if a.role:
            l.setObjectName(a.role)
        l.show()
        if a.fight:
            QTimer.singleShot(300, lambda l=l: l.resize(W, H))
        wins.append(l)
    sys.exit(app.exec())
