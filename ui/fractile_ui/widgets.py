"""Shared widgets: shortcut capture with conflict feedback, grid picker."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSpinBox, QToolButton, QVBoxLayout, QWidget

from . import keys
from .grid_model import GridModel, Sel
from .gridwidget import GridWidget

RESERVED_LOCAL = ["Left", "Right", "Up", "Down", "Shift+Left", "Shift+Right", "Shift+Up", "Shift+Down", "Return", "KP_Enter", "space", "Escape", "Tab", "Shift+Tab",
                  "Alt+plus", "Alt+minus", "Alt+Shift+plus", "Alt+Shift+minus", "Alt+equal", "Alt+Shift+equal", "Alt+KP_Add", "Alt+KP_Subtract"]


class ShortcutEdit(QWidget):
    """Click, press keys. Shows whether the combination is free (DV-26)."""
    changed = pyqtSignal(object)  # str | None

    def __init__(self, app, scope: str = "global", value: str | None = None, own_id: str | None = None, parent=None):
        super().__init__(parent)
        self.app, self.scope, self.value, self.own_id = app, scope, value, own_id
        self.capturing = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.btn = QPushButton()
        self.btn.setCheckable(True)
        self.btn.setMinimumWidth(180)
        self.btn.clicked.connect(self.toggle)
        self.btn.setAccessibleName("Shortcut")
        clear = QToolButton()
        clear.setText("Clear")
        clear.clicked.connect(lambda: self.set_value(None))
        row.addWidget(self.btn)
        row.addWidget(clear)
        row.addStretch(1)
        lay.addLayout(row)
        self.info = QLabel()
        self.info.setWordWrap(True)
        # Room for a two-line message from the start: a wrapped label that
        # grows later does not grow a shown dialog, and the rows below it
        # (the grid picker) were squeezed.
        self.info.setMinimumHeight(2 * self.info.fontMetrics().lineSpacing() + 2)
        lay.addWidget(self.info)
        self._render()

    def toggle(self):
        self.capturing = self.btn.isChecked()
        if self.capturing:
            self.btn.setText("Press keys…  (Esc to cancel)")
            self._pause(True)
            self.grabKeyboard()
        else:
            self.releaseKeyboard()
            self._pause(False)
            self._render()

    def _pause(self, on: bool) -> None:
        # Global shortcuts are paused while listening, so the desktop hands
        # the keys of an active Fractile shortcut to this editor (on Wayland
        # an application cannot grab the keyboard). Only global scope: panel
        # shortcuts are not registered with the desktop.
        if self.scope != "global" and on:
            return
        self.app.bridge.try_call("hotkeys_pause" if on else "hotkeys_resume", {"seconds": 60} if on else {})

    def setScope(self, scope: str) -> None:  # noqa: N802
        if self.capturing and scope != "global":
            self._pause(False)
        self.scope = scope
        if self.capturing and scope == "global":
            self._pause(True)

    def hideEvent(self, ev):  # noqa: N802
        if self.capturing:
            self.capturing = False
            self.btn.setChecked(False)
            self.releaseKeyboard()
            self._pause(False)
        super().hideEvent(ev)

    def keyPressEvent(self, ev):
        if not self.capturing:
            return super().keyPressEvent(ev)
        if ev.key() == Qt.Key.Key_Escape and not ev.modifiers():
            self.btn.setChecked(False)
            self.toggle()
            return
        acc = keys.accel_from_event(ev)
        if acc is None:
            return
        bad = self.static_problem(acc)
        if bad:
            # Keep the previous value and keep listening.
            self.info.setText("\u26a0 " + bad + " Try another combination, or Esc to keep the current one.")
            return
        self.btn.setChecked(False)
        self.capturing = False
        self.releaseKeyboard()
        self._pause(False)
        self.set_value(acc)

    def static_problem(self, acc: str) -> str | None:
        """Problems detectable without asking the service."""
        if self.scope == "local":
            if any(keys.same(acc, r) for r in RESERVED_LOCAL):
                return "The panel uses this key for keyboard navigation."
            return None
        mods, _ = keys.normalize(acc) or (frozenset(), "")
        if not mods:
            return "Global shortcuts need at least one modifier (Ctrl, Alt, Shift or Super)."
        return None

    def set_value(self, v: str | None):
        self.value = v
        self._render()
        self.changed.emit(v)

    def _render(self):
        self.btn.setText(self.value or "Not assigned")
        self.info.setText("")
        if not self.value:
            return
        if self.scope == "local":
            if any(keys.same(self.value, r) for r in RESERVED_LOCAL):
                self.info.setText("⚠ The panel uses this key for keyboard navigation. Pick another.")
                return
            for s in (self.app.config() or {}).get("shortcuts", []):
                if s.get("id") != self.own_id and s.get("scope") == "local" and s.get("accelerator") and keys.same(s["accelerator"], self.value):
                    self.info.setText(f"⚠ Already used by panel shortcut “{s['name']}”.")
                    return
            self.info.setText("✓ Panel shortcut: only active while the panel is open.")
            return
        mods, _ = keys.normalize(self.value) or (frozenset(), "")
        if not mods:
            self.info.setText("⚠ Global shortcuts need at least one modifier (Ctrl, Alt, Shift or Super).")
            return
        for s in (self.app.config() or {}).get("shortcuts", []):
            if s.get("id") != self.own_id and s.get("scope") == "global" and s.get("accelerator") and keys.same(s["accelerator"], self.value):
                self.info.setText(f"⚠ Already used by “{s['name']}”.")
                return
        r = self.app.bridge.try_call("shortcut_check", {"accelerator": self.value, "own_id": self.own_id})
        if r is None:
            self.info.setText("Could not check (service not running).")
        elif r.get("free") and r.get("reason") == "current binding (active)":
            self.info.setText("\u2713 Active.")
        elif r.get("free"):
            self.info.setText("✓ Available. Note: some desktop shortcuts cannot be detected until they are pressed.")
        else:
            self.info.setText("⚠ " + str(r.get("reason")))

    def is_ok(self) -> bool:
        return not self.info.text().startswith("⚠")


class GridPicker(QWidget):
    """Pick a grid size and a cell rectangle (for shortcuts and rules)."""
    changed = pyqtSignal()

    def __init__(self, grid: dict | None = None, sel: dict | None = None, parent=None):
        super().__init__(parent)
        g = grid or {"cols": 6, "rows": 6}
        cols, rows = g["cols"], g["rows"]
        # The picker edits effective grids up to 20x20 directly (no Alt mode).
        self.model = GridModel(cols, rows)
        if sel:
            self.model.selection = Sel(sel["col0"], sel["row0"], sel["col1"], sel["row1"])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.c = QSpinBox()
        self.c.setRange(1, 20)
        self.c.setValue(cols)
        self.r = QSpinBox()
        self.r.setRange(1, 20)
        self.r.setValue(rows)
        row.addWidget(QLabel("Columns"))
        row.addWidget(self.c)
        row.addWidget(QLabel("Rows"))
        row.addWidget(self.r)
        row.addStretch(1)
        lay.addLayout(row)
        self.gw = GridWidget(self.model, 24)
        self.gw.applied.connect(lambda *_: self.changed.emit())
        lay.addWidget(self.gw)
        self.c.valueChanged.connect(self._resize)
        self.r.valueChanged.connect(self._resize)
        self._resize()

    def _resize(self):
        self.model.cols, self.model.rows = self.c.value(), self.r.value()
        if self.model.selection and (self.model.selection.col1 >= self.model.cols or self.model.selection.row1 >= self.model.rows):
            self.model.selection = None
            self.model.anchor = None
        cpx = max(12, min(28, 240 // max(self.model.cols, self.model.rows)))
        self.gw.cell_px = cpx
        self.gw.setFixedSize(self.model.cols * cpx + 1, self.model.rows * cpx + 1)
        self.gw.update()
        self.changed.emit()

    def grid(self) -> dict:
        return self.model.eff_grid()

    def selection(self) -> dict | None:
        return self.model.selection.as_dict() if self.model.selection else None
