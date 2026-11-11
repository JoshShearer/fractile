"""The placement panel (DV-02..DV-13, DV-15, DV-21, DV-24, DV-27, DV-28).

One panel per monitor (or only the target's monitor). The target window is
captured by the service *before* the panel takes focus, so selecting cells
never acts on Fractile's own window.
"""
from __future__ import annotations

import json
import os

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QIcon, QKeyEvent
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout

from . import keys
from .grid_model import GridModel, Sel
from .gridwidget import GridWidget
from .overlay import PreviewOverlay
from .theme import mute


def output_center(o: dict) -> tuple[int, int]:
    r = o["rect"]
    return r["x"] + r["w"] // 2, r["y"] + r["h"] // 2


def output_of_frame(outputs: list[dict], frame: dict | None) -> dict | None:
    if not outputs:
        return None
    if frame:
        best, area = None, -1
        for o in outputs:
            r = o["rect"]
            ix = max(0, min(r["x"] + r["w"], frame["x"] + frame["w"]) - max(r["x"], frame["x"]))
            iy = max(0, min(r["y"] + r["h"], frame["y"] + frame["h"]) - max(r["y"], frame["y"]))
            if ix * iy > area:
                best, area = o, ix * iy
        if area > 0:
            return best
    return next((o for o in outputs if o.get("primary")), outputs[0])


def screen_for_output(o: dict):
    """QScreen showing this output (device pixels -> logical)."""
    r = o["rect"]
    for s in QGuiApplication.screens():
        g, dpr = s.geometry(), s.devicePixelRatio()
        if round(g.x() * dpr) == r["x"] and round(g.y() * dpr) == r["y"]:
            return s
        if g.x() == r["x"] and g.y() == r["y"]:
            return s
    cx, cy = output_center(o)
    for s in QGuiApplication.screens():
        dpr = s.devicePixelRatio()
        from PyQt6.QtCore import QPoint
        if s.geometry().contains(QPoint(int(cx / dpr), int(cy / dpr))):
            return s
    return QGuiApplication.primaryScreen()


def to_logical(rect: dict, screen) -> QRect:
    dpr = screen.devicePixelRatio() if screen else 1.0
    g = screen.geometry() if screen else QRect()
    # Device-pixel rect on this screen -> logical coordinates.
    if dpr == 1.0:
        return QRect(rect["x"], rect["y"], rect["w"], rect["h"])
    ox = round(g.x() * dpr)
    oy = round(g.y() * dpr)
    return QRect(g.x() + round((rect["x"] - ox) / dpr), g.y() + round((rect["y"] - oy) / dpr), round(rect["w"] / dpr), round(rect["h"] / dpr))


class PanelWindow(QFrame):
    closed = pyqtSignal()

    def __init__(self, ctl: "PanelController", output: dict):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.ctl = ctl
        self.output = output
        self.setObjectName("FractilePanel")
        self.setWindowTitle(f"Fractile panel {output['stable_id']}")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("#FractilePanel { border-radius: 12px; border: 1px solid palette(mid); background: palette(window); }")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)
        # header: target app
        h = QHBoxLayout()
        self.icon = QLabel()
        self.icon.setFixedSize(32, 32)
        self.app = QLabel()
        f = self.app.font()
        f.setBold(True)
        self.app.setFont(f)
        self.title = QLabel()
        mute(self.title)
        tv = QVBoxLayout()
        tv.setSpacing(0)
        tv.addWidget(self.app)
        tv.addWidget(self.title)
        h.addWidget(self.icon)
        h.addLayout(tv, 1)
        self.mon = QLabel()
        mute(self.mon, small=True)
        h.addWidget(self.mon, 0, Qt.AlignmentFlag.AlignTop)
        close = QToolButton()
        close.setText("✕")
        close.setToolTip("Close (Esc)")
        close.setAutoRaise(True)
        close.clicked.connect(ctl.cancel)
        h.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(h)
        # grid
        self.grid = GridWidget(ctl.model, ctl.cell_px)
        self.grid.preview.connect(self.on_preview)
        self.grid.applied.connect(lambda sel, shift: ctl.apply(self.output, sel, shift))
        self.grid.installEventFilter(self)
        v.addWidget(self.grid, 0, Qt.AlignmentFlag.AlignHCenter)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        v.addWidget(self.status)
        # footer: grid size + quick actions
        foot = QHBoxLayout()
        self.size_lbl = QLabel()
        self.size_lbl.setToolTip("Alt+Plus / Alt+Minus: columns\nAlt+Shift+Plus / Alt+Shift+Minus: rows")
        def mk(text, tip, fn):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(fn)
            return b
        foot.addWidget(mk("−", "Fewer columns (Alt+- or Alt+Keypad -)", lambda: ctl.resize(-1, 0)))
        foot.addWidget(self.size_lbl)
        foot.addWidget(mk("+", "More columns (Alt+= or Alt+Keypad +)", lambda: ctl.resize(1, 0)))
        foot.addSpacing(6)
        foot.addWidget(mk("↕−", "Fewer rows (Alt+Shift+-)", lambda: ctl.resize(0, -1)))
        foot.addWidget(mk("↕+", "More rows (Alt+Shift+=)", lambda: ctl.resize(0, 1)))
        foot.addStretch(1)
        self.favs = QHBoxLayout()
        foot.addLayout(self.favs)
        foot.addWidget(mk("↶", "Undo last placement", ctl.undo))
        foot.addWidget(mk("⚙", "Settings", lambda: ctl.app.show_settings()))
        foot.addWidget(mk("▦", "Workspace dashboard", lambda: ctl.app.show_dashboard()))
        v.addLayout(foot)
        self.help = QLabel("Drag to place  ·  Alt: finer grid  ·  Shift: keep open  ·  Esc: close")
        mute(self.help, small=True)
        v.addWidget(self.help)

    # ---- content ----
    def refresh(self, payload: dict, cfg: dict):
        t = payload.get("target")
        if t:
            name = payload.get("target_app_name") or t.get("desktop_file_id") or t.get("wm_class") or "Window"
            self.app.setText(str(name).removesuffix(".desktop"))
            title = t.get("title") or ""
            self.title.setText(self.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, 260))
            icon_name = payload.get("target_icon")
            ic = QIcon.fromTheme(icon_name) if icon_name and not icon_name.startswith("/") else QIcon(icon_name or "")
            if ic.isNull():
                ic = QIcon.fromTheme("application-x-executable")
            self.icon.setPixmap(ic.pixmap(QSize(32, 32)))
            self.set_target_ok(True)
        else:
            self.app.setText("No window selected")
            self.title.setText("Focus a window, then open the panel again.")
            self.icon.clear()
            self.set_target_ok(False)
        multi = len(payload.get("outputs") or []) > 1
        self.mon.setText((self.output.get("description") or self.output.get("name") or "") if multi else "")
        self.help.setVisible(bool(cfg.get("general", {}).get("show_help", True)))
        while self.favs.count():
            w = self.favs.takeAt(0).widget()
            if w:
                w.deleteLater()
        for s in [s for s in cfg.get("shortcuts", []) if s.get("favorite")][:6]:
            b = QPushButton(s["name"][:14])
            b.setToolTip(s["name"])
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setFlat(True)
            b.clicked.connect(lambda _=False, sid=s["id"]: self.ctl.run_shortcut(sid, self.output))
            self.favs.addWidget(b)
        self.update_size_label()

    def set_target_ok(self, ok: bool, message: str = ""):
        self.grid.enabled_for_target = ok
        self.status.setVisible(bool(message))
        self.status.setText(message)
        self.grid.update()

    def show_status(self, message: str):
        self.status.setText(message)
        self.status.setVisible(bool(message))

    def update_size_label(self):
        m = self.ctl.model
        txt = f"{m.cols} × {m.rows}"
        if m.subdivided:
            txt += f"  ({m.eff_cols} × {m.eff_rows})"
        self.size_lbl.setText(txt)
        self.grid.refresh_size()
        self.adjustSize()

    def place_on_output(self):
        scr = screen_for_output(self.output)
        wa = to_logical(self.output["work_area"], scr)
        self.adjustSize()
        sz = self.sizeHint()
        x = wa.x() + (wa.width() - sz.width()) // 2
        y = wa.y() + (wa.height() - sz.height()) // 2
        self.move(x, y)
        return QRect(x, y, sz.width(), sz.height()), scr

    # ---- preview ----
    def on_preview(self, sel: Sel | None):
        self.ctl.preview(self.output, sel)

    # ---- keys ----
    def eventFilter(self, obj, ev):
        if isinstance(ev, QKeyEvent):
            if ev.type() == QKeyEvent.Type.KeyPress and self.ctl.key_press(self, ev):
                return True
            if ev.type() == QKeyEvent.Type.KeyRelease and self.ctl.key_release(self, ev):
                return True
        return super().eventFilter(obj, ev)

    def keyPressEvent(self, ev):
        if not self.ctl.key_press(self, ev):
            super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev):
        if not self.ctl.key_release(self, ev):
            super().keyReleaseEvent(ev)

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)


class PanelController:
    def __init__(self, app):
        self.app = app
        self.model = GridModel(6, 6)
        self.cell_px = 40
        self.panels: list[PanelWindow] = []
        self.overlay = PreviewOverlay()
        self.payload: dict = {}
        self.cfg: dict = {}
        self.visible = False
        self._preview_cache: dict = {}
        self._last_grid_cfg = None

    # ---- lifecycle ----
    def show(self, payload: dict):
        self.cfg = self.app.config() or {}
        appearance = self.cfg.get("appearance", {})
        g = payload.get("grid") or appearance.get("default_grid") or {"cols": 6, "rows": 6}
        grid_cfg = (g["cols"], g["rows"])
        if not self.visible:
            if appearance.get("reset_grid_on_open", False) or self._last_grid_cfg != grid_cfg:
                self.model.cols, self.model.rows = grid_cfg
            self._last_grid_cfg = grid_cfg
            self.model.reset()
        self.cell_px = int(appearance.get("cell_size", 40))
        self.payload = payload
        outputs = payload.get("outputs") or []
        target = payload.get("target")
        home = output_of_frame(outputs, target.get("frame") if target else None)
        wanted = outputs if self.cfg.get("general", {}).get("panel_on_all_monitors", True) else ([home] if home else [])
        for p in self.panels:
            p.hide()
            p.deleteLater()
        self.panels = []
        self._preview_cache.clear()
        for o in wanted:
            p = PanelWindow(self, o)
            p.grid.cell_px = self.cell_px
            p.refresh(payload, self.cfg)
            self.panels.append(p)
        focus_panel = next((p for p in self.panels if home and p.output["stable_id"] == home["stable_id"]), self.panels[0] if self.panels else None)
        for p in self.panels:
            rect, _ = p.place_on_output()
            if p is not focus_panel:
                p.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            p.show()
            self.app.position_own(p, rect)
        self.visible = True
        self.app.register_own_windows()
        if focus_panel:
            focus_panel.raise_()
            focus_panel.activateWindow()
            focus_panel.grid.setFocus()
            QTimer.singleShot(0, lambda: self.app.focus_own(focus_panel))
        self.app.bridge.try_call("panel_visible", {"visible": True})
        if os.environ.get("FRACTILE_UI_TEST_GEOMETRY"):
            QTimer.singleShot(150, self._dump_geometry)

    def _dump_geometry(self):
        """Test hook (only with FRACTILE_UI_TEST_GEOMETRY set): record where
        each panel's grid is on screen so GUI tests can drag real cells."""
        out = []
        for p in self.panels:
            tl = p.grid.mapToGlobal(QPoint(0, 0))
            out.append({"output": p.output.get("stable_id"), "x": tl.x(), "y": tl.y(), "w": p.grid.width(), "h": p.grid.height(),
                        "cols": self.model.eff_cols, "rows": self.model.eff_rows, "active": p.isActiveWindow()})
        try:
            with open(os.environ["FRACTILE_UI_TEST_GEOMETRY"], "w") as f:
                json.dump(out, f)
        except OSError:
            pass

    def hide(self, notify: bool = True):
        self.overlay.hide()
        for p in self.panels:
            p.hide()
        was = self.visible
        self.visible = False
        self.model.set_subdivided(False)
        if notify and was:
            self.app.bridge.try_call("panel_visible", {"visible": False})
        if was:
            QTimer.singleShot(400, self.app.maybe_show_wizard)

    def cancel(self):
        """Escape: dismiss with no side effects (DV-09)."""
        self.hide()
        self.return_focus()

    def return_focus(self):
        t = (self.payload or {}).get("target")
        if t:
            self.app.bridge.try_call("activate_window", {"window": t["id"]})

    def target_closed(self, still_exists: bool):
        if not self.visible:
            return
        if not still_exists:
            for p in self.panels:
                p.set_target_ok(False, "The window you opened the panel for was closed. Focus another window and open the panel again.")
            self.overlay.hide()

    # ---- grid size / subdivision ----
    def resize(self, dc: int, dr: int):
        if self.model.resize(dc, dr):
            for p in self.panels:
                p.update_size_label()
                p.place_on_output()
            self._preview_cache.clear()
            self.overlay.hide()

    def set_subdivided(self, on: bool):
        if on and not self.cfg.get("appearance", {}).get("subdivision_enabled", True):
            return
        if self.model.subdivided == on:
            return
        self.model.set_subdivided(on)
        for p in self.panels:
            p.update_size_label()
        if self.model.selection is None:
            self.overlay.hide()
        else:
            focus = next((p for p in self.panels if p.isActiveWindow()), None)
            if focus:
                self.preview(focus.output, self.model.selection)

    # ---- preview ----
    def preview(self, output: dict, sel: Sel | None):
        if sel is None or not self.cfg.get("appearance", {}).get("preview_enabled", True) or not (self.payload or {}).get("target"):
            self.overlay.hide()
            return
        key = (output["stable_id"], self.model.eff_cols, self.model.eff_rows, sel)
        r = self._preview_cache.get(key)
        if r is None:
            r = self.app.bridge.try_call("compute_rect", {"output": output["stable_id"], "grid": self.model.eff_grid(), "selection": sel.as_dict()})
            if r is None:
                return
            self._preview_cache[key] = r
        scr = screen_for_output(output)
        self.overlay.show_rect(to_logical(r, scr), f"{r['w']} × {r['h']}")
        self.app.register_own_windows()
        for p in self.panels:
            p.raise_()

    # ---- apply ----
    def apply(self, output: dict, sel: Sel, shift: bool):
        t = (self.payload or {}).get("target")
        if not t:
            return
        dismiss = bool(self.cfg.get("general", {}).get("dismiss_after_apply", True)) != bool(shift)
        params = {"window": t["id"], "output": output["stable_id"], "grid": self.model.eff_grid(), "selection": sel.as_dict(), "from_panel": True}
        if dismiss:
            self.hide()
        else:
            self.overlay.hide()
            self.model.anchor = None
            self.model.selection = None
            for p in self.panels:
                p.grid.update()

        def ok(res):
            if dismiss:
                self.return_focus()
            if res and not res.get("within_tolerance", True):
                note = res.get("note") or ""
                self.app.notify("Placed approximately", f"The window settled {res.get('max_error_px')} px from the target. {note}".strip())
            if not dismiss:
                for p in self.panels:
                    p.show_status("")

        def fail(code, msg):
            if dismiss:
                self.app.notify("Could not place window", msg)
            else:
                for p in self.panels:
                    p.show_status(msg)

        self.app.bridge.call_async("place", params, ok, fail)

    def run_shortcut(self, sid: str, output: dict | None = None):
        dismiss = bool(self.cfg.get("general", {}).get("dismiss_after_apply", True))
        if dismiss:
            self.hide()
        self.app.bridge.call_async("run_shortcut", {"id": sid}, lambda _r: dismiss and self.return_focus(), lambda c, m: self.app.notify("Shortcut failed", m))

    def undo(self):
        self.app.bridge.call_async("undo", {}, None, lambda c, m: self.app.notify("Nothing to undo", m))

    # ---- keyboard ----
    def key_press(self, panel: PanelWindow, ev) -> bool:
        k = ev.key()
        mods = ev.modifiers()
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if k == Qt.Key.Key_Escape:
            self.cancel()
            return True
        if k == Qt.Key.Key_Alt and not ev.isAutoRepeat():
            self.set_subdivided(True)
            return True
        if alt and k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.resize(0, 1) if shift else self.resize(1, 0)
            return True
        if alt and k in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.resize(0, -1) if shift else self.resize(-1, 0)
            return True
        arrows = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if k in arrows and not ctrl and not alt:
            if not panel.grid.enabled_for_target:
                return True
            dx, dy = arrows[k]
            sel = self.model.key_move(dx, dy, shift)
            panel.grid.update()
            self.preview(panel.output, sel)
            return True
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and not ctrl and not alt:
            if self.model.selection is not None and panel.grid.enabled_for_target:
                self.apply(panel.output, self.model.selection, shift)
            return True
        # Panel-local named shortcuts (DV-15).
        acc = keys.accel_from_event(ev)
        if acc:
            for s in self.cfg.get("shortcuts", []):
                if s.get("scope") == "local" and s.get("accelerator") and keys.same(s["accelerator"], acc):
                    self.run_shortcut(s["id"], panel.output)
                    return True
        return False

    def key_release(self, panel: PanelWindow, ev) -> bool:
        if ev.key() == Qt.Key.Key_Alt and not ev.isAutoRepeat():
            self.set_subdivided(False)
            return True
        return False
