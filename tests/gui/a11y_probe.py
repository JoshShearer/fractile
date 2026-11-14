#!/usr/bin/env python3
"""In-process accessibility checks on the real UI (UX-06), run by
a11y-in-container.sh against a live service on an X display.

Opens every window the way the service asks for it (the placement panel
through `fractile panel show`, the rest through the app's own entry points)
and checks what assistive technology is told about it:

  A1  every control a keyboard user can reach has an accessible name, read
      through QAccessible (what Qt hands to AT-SPI and so to Orca)
  A2  the high-contrast theme: text and selection colours reach 7:1 (WCAG
      AAA) and the panel is drawn opaque
  A3  font scaling to 150%: the font grows and no label or button is cut
      short in any window
  A4  right-to-left (run with an RTL locale, --rtl): the windows mirror, but
      the grid keeps screen order, column 0 on the left as on the monitor

Prints one JSON object: {"results": [...]}.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))

from PyQt6.QtCore import QPoint, Qt, QTimer  # noqa: E402
from PyQt6.QtGui import QColor, QPalette  # noqa: E402
from PyQt6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSlider, QAbstractSpinBox, QApplication,  # noqa: E402
                             QComboBox, QLabel, QLineEdit, QTabBar, QTabWidget, QWidget)

from fractile_ui.app import FractileApp  # noqa: E402

results: list[dict] = []


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


def describe(w: QWidget) -> str:
    t = getattr(w, "text", None)
    t = t() if callable(t) else ""
    return f"{w.metaObject().className()}{'(' + t + ')' if t else ''}{'#' + w.objectName() if w.objectName() else ''}"


def windows(app: FractileApp) -> list[tuple[str, QWidget, list]]:
    """(label, window, pages) for each window; pages are callables that show
    one page of a paged window."""
    out = []
    s = app.settings_win
    out.append(("settings", s, [lambda i=i: s.tabs.setCurrentIndex(i) for i in range(s.tabs.count())]))
    out.append(("dashboard", app.dashboard_win, [None]))
    out.append(("recovery", app.recovery_win, [None]))
    wz = app.wizard_win
    if wz is not None:
        def page(i):
            wz.restart()
            for _ in range(i):
                wz.next()
        out.append(("wizard", wz, [lambda i=i: page(i) for i in range(len(wz.pageIds()))]))
    out.append(("palette", app.palette_win, [None]))
    for n, p in enumerate(app.panel.panels):
        out.append((f"panel{n}", p, [None]))
    return out


def open_all(app: FractileApp) -> bool:
    subprocess.run(["fractile", "panel", "show"], capture_output=True, timeout=20)
    if not wait(lambda: app.panel.visible and app.panel.panels and app.panel.panels[0].isVisible(), 8.0):
        return False
    pump(0.5)
    app.show_settings("general")
    app.show_dashboard()
    app.show_recovery({"reason": "ask_each_login", "snapshot_id": None})
    app.show_wizard()
    app.show_palette()
    pump(1.5)
    return True


def atspi(win: QWidget) -> dict:
    """What a screen reader sees in this window: the reader runs in its own
    process (this one has to keep answering AT-SPI while it looks)."""
    pr = subprocess.Popen([sys.executable, str(ROOT / "tests" / "tools" / "atspi_names.py"), str(os.getpid()), win.windowTitle()],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    end = time.time() + 30
    while pr.poll() is None and time.time() < end:
        pump(0.05)
    if pr.poll() is None:
        pr.kill()
        return {"found": False, "error": "timed out"}
    try:
        return json.loads(pr.stdout.read().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"found": False, "error": pr.stderr.read()[-300:]}


def a1(app: FractileApp) -> None:
    missing, total, notfound = [], 0, []
    for label, win, pages in windows(app):
        if win is None:
            notfound.append(f"{label}: did not open")
            continue
        win.show()
        for k, page in enumerate(pages):
            if page:
                page()
            win.raise_()
            pump(0.4)
            # inner tabs (Session restore has four) are hidden until chosen
            inner = [t for t in win.findChildren(QTabWidget) if t.isVisible() and t is not getattr(win, "tabs", None)]
            steps = [(t, i) for t in inner for i in range(t.count())] or [(None, 0)]
            for t, i in steps:
                if t is not None:
                    t.setCurrentIndex(i)
                    pump(0.3)
                where = f"{label}[{k}{'.' + str(i) if t is not None else ''}]"
                r = atspi(win)
                if not r.get("found"):
                    notfound.append(f"{where} '{win.windowTitle()}' {r.get('error', '')} apps={r.get('apps')}")
                    continue
                total += r["controls"]
                missing += [f"{where} {u['role']} {u['path']}" for u in r["unnamed"]]
    check("A1", "every control a keyboard user can reach has a name in the AT-SPI tree (what Orca announces)",
          total > 40 and not missing and not notfound,
          f"{total} controls; unnamed: {missing[:25]}{' …' if len(missing) > 25 else ''}; windows not found: {notfound}")


def contrast(a: QColor, b: QColor) -> float:
    def lum(c: QColor) -> float:
        ch = []
        for v in (c.redF(), c.greenF(), c.blueF()):
            ch.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def a2(app: FractileApp, qapp: QApplication) -> None:
    # the way a user picks it: Settings > Appearance > Theme, saved by the service
    app.bridge.try_call("set_config", {"patch": {"appearance": {"theme": "high_contrast"}}})
    wait(lambda: app.config().get("appearance", {}).get("theme") == "high_contrast", 5.0)
    subprocess.run(["fractile", "panel", "show"], capture_output=True, timeout=20)
    pump(1.0)
    pal = qapp.palette()
    R = QPalette.ColorRole
    pairs = {"window text": (R.WindowText, R.Window), "field text": (R.Text, R.Base), "button text": (R.ButtonText, R.Button),
             "selection": (R.HighlightedText, R.Highlight)}
    ratios = {k: round(contrast(pal.color(f), pal.color(b)), 1) for k, (f, b) in pairs.items()}
    p = app.panel.panels[0] if app.panel.panels else None
    opaque, hc = None, None
    if p:
        hc = p.high_contrast()
        img = p.grab().toImage()
        # grab() of a translucent window keeps its alpha: sample inside the rounded corners
        pts = [QPoint(img.width() // 2, 4), QPoint(12, img.height() // 2), QPoint(img.width() // 2, img.height() // 2)]
        opaque = all(img.pixelColor(pt).alpha() == 255 for pt in pts)
    app.bridge.try_call("set_config", {"patch": {"appearance": {"theme": "system"}}})
    wait(lambda: app.config().get("appearance", {}).get("theme") == "system", 5.0)
    subprocess.run(["fractile", "panel", "show"], capture_output=True, timeout=20)
    pump(0.8)
    check("A2", "high-contrast theme chosen in settings: text and selection reach 7:1, and the panel is drawn opaque",
          all(v >= 7 for v in ratios.values()) and hc is True and opaque is True, f"contrast {ratios}; panel high contrast {hc}, opaque {opaque}")


def clipped(win: QWidget) -> list[str]:
    out = []
    for w in win.findChildren(QWidget):
        if not w.isVisible():
            continue
        if isinstance(w, QLabel) and not w.wordWrap() and w.text():
            if w.width() + 1 < w.minimumSizeHint().width():
                out.append(f"{describe(w)} {w.width()}<{w.minimumSizeHint().width()}")
        elif isinstance(w, QAbstractButton) and w.text():
            if w.width() + 1 < w.minimumSizeHint().width() or w.height() + 1 < w.fontMetrics().height():
                out.append(f"{describe(w)} {w.width()}x{w.height()} < {w.minimumSizeHint().width()}")
    return out


def a3(app: FractileApp, qapp: QApplication) -> None:
    base = qapp.font().pointSizeF()
    app.bridge.try_call("set_config", {"patch": {"appearance": {"font_scale": 1.5}}})
    grown = wait(lambda: qapp.font().pointSizeF() if qapp.font().pointSizeF() > base else None, 5.0) or qapp.font().pointSizeF()
    pump(0.5)
    cut = []
    for label, win, pages in windows(app):
        if win is None or not win.isVisible() or label.startswith("wizard-page"):
            continue
        for k, page in enumerate(pages):
            if page:
                page()
            pump(0.3)
            cut += [f"{label}[{k}] {c}" for c in clipped(win)]
    app.bridge.try_call("set_config", {"patch": {"appearance": {"font_scale": 1.0}}})
    pump(0.5)
    check("A3", "font scale 150% chosen in settings: the font grows and no label or button in any window is cut short",
          abs(grown - base * 1.5) < 0.6 and not cut, f"font {base} -> {grown} pt; cut: {cut[:20]}")


def a4(app: FractileApp, qapp: QApplication) -> None:
    rtl = qapp.layoutDirection() == Qt.LayoutDirection.RightToLeft
    p = app.panel.panels[0] if app.panel.panels else None
    detail, mirrored, grid_ltr, col0_left = [], False, False, False
    if p:
        close = p.findChild(QAbstractButton, "close")
        if close:
            mirrored = close.mapTo(p, QPoint(0, 0)).x() < p.width() // 2
            detail.append(f"close button at x={close.mapTo(p, QPoint(0, 0)).x()} of {p.width()}")
        g = p.grid
        grid_ltr = g.layoutDirection() == Qt.LayoutDirection.LeftToRight
        cell = getattr(g, "cell_at", None)
        if cell:
            c = cell(QPoint(3, 3))
            col0_left = c is not None and c[0] == 0
            detail.append(f"cell at the left edge {c}")
    s = app.settings_win
    form_mirrored = None
    if s:
        s.tabs.setCurrentIndex(0)
        pump(0.2)
        form_mirrored = s.tabs.tabBar().tabRect(0).x() > s.tabs.tabBar().tabRect(1).x()
        detail.append(f"first settings tab right of the second {form_mirrored}")
    check("A4", "right-to-left locale: windows mirror (panel controls, settings tabs) while the grid keeps screen order",
          rtl and mirrored and bool(form_mirrored) and grid_ltr and col0_left,
          f"app RTL {rtl}; grid LTR {grid_ltr}; column 0 on the left {col0_left}; " + "; ".join(detail))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtl", action="store_true", help="only the right-to-left check (run under an RTL locale)")
    a = ap.parse_args()
    qapp = QApplication(sys.argv[:1])
    qapp.setQuitOnLastWindowClosed(False)
    args = argparse.Namespace(settings=False, page=None, launcher=True, dashboard=False, panel=False, palette=False)
    app = FractileApp(qapp, args)
    app.start()

    def go():
        try:
            if not wait(lambda: app.bridge.calls is not None, 10.0):
                check("AXX", "the UI connected to the service", False)
            elif not open_all(app):
                check("AXX", "the panel opened", False, f"visible {app.panel.visible}")
            elif a.rtl:
                a4(app, qapp)
            else:
                a1(app)
                a2(app, qapp)
                a3(app, qapp)
        except Exception as ex:  # noqa: BLE001
            import traceback
            check("AXX", "probe crashed", False, traceback.format_exc()[-1200:])
        qapp.quit()

    QTimer.singleShot(300, go)
    qapp.exec()
    print(json.dumps({"results": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
