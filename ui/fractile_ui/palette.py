"""Command palette: type to find any placement, layout or session action."""
from __future__ import annotations

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QCursor, QGuiApplication
from PyQt6.QtWidgets import QFrame, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

from . import a11y
from .settings import SNAP_ACTIONS


class CommandPalette(QFrame):
    def __init__(self, app):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.app = app
        self.setWindowTitle("Fractile command palette")
        self.setObjectName("FractilePalette")
        self.setStyleSheet("#FractilePalette { border-radius: 12px; border: 1px solid palette(mid); background: palette(window); }")
        v = QVBoxLayout(self)
        self.q = a11y.name(QLineEdit(), "Command", "Type to filter the commands below; Enter runs the selected one")
        self.q.setPlaceholderText("Type a command: left half, layout, save, restore…")
        self.q.textChanged.connect(self.filter)
        self.q.returnPressed.connect(self.run_current)
        self.q.installEventFilter(self)
        v.addWidget(self.q)
        self.list = a11y.name(QListWidget(), "Commands")
        self.list.itemActivated.connect(lambda _i: self.run_current())
        self.list.installEventFilter(self)
        v.addWidget(self.list)
        self.resize(520, 380)
        self.items: list[tuple[str, callable]] = []

    def build(self):
        b = self.app.bridge
        items = []
        for a in SNAP_ACTIONS:
            items.append((f"Snap: {a.replace('-', ' ')}", lambda a=a: b.call_async("snap", {"action": a})))
        items += [("Move window to next monitor", lambda: b.call_async("move_to_output", {"step": 1})),
                  ("Move window to previous monitor", lambda: b.call_async("move_to_output", {"step": -1})),
                  ("Undo last placement", lambda: b.call_async("undo", {})),
                  ("Show panel", lambda: b.call_async("show_panel", {})),
                  ("Save workspace now", self.app.save_now),
                  ("Restore latest snapshot…", lambda: self.app.show_recovery({"snapshot_id": None, "reason": "palette"})),
                  ("Undo last restore", lambda: b.call_async("restore_undo", {})),
                  ("Workspace dashboard", self.app.show_dashboard),
                  ("Settings", self.app.show_settings)]
        for s in (self.app.config() or {}).get("shortcuts", []):
            items.append((f"Shortcut: {s['name']}", lambda sid=s["id"]: b.call_async("run_shortcut", {"id": sid})))
        for l in b.try_call("layout_list") or []:
            items.append((f"Layout: apply {l['name']}", lambda n=l["name"]: self.app.show_recovery({"layout": n, "reason": "palette"})))
        self.items = items

    def popup(self):
        self.build()
        self.q.clear()
        self.filter("")
        scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        g = scr.availableGeometry()
        x, y = g.x() + (g.width() - self.width()) // 2, g.y() + g.height() // 4
        self.move(x, y)
        self.show()
        if not self.app.wayland:
            # Frameless windows on a second monitor can be mapped elsewhere
            # on GNOME Xorg; position_own moves them back (see there).
            self.app.position_own(self, QRect(x, y, self.width(), self.height()))
        self.raise_()
        self.activateWindow()
        self.q.setFocus()

    def filter(self, text: str):
        words = text.lower().split()
        self.list.clear()
        for label, fn in self.items:
            if all(w in label.lower() for w in words):
                it = QListWidgetItem(label)
                it.setData(Qt.ItemDataRole.UserRole, fn)
                self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def run_current(self):
        it = self.list.currentItem()
        if not it:
            return
        fn = it.data(Qt.ItemDataRole.UserRole)
        self.hide()
        fn()

    def eventFilter(self, obj, ev):
        if ev.type() != ev.Type.KeyPress:
            return super().eventFilter(obj, ev)
        key = ev.key()
        if key == Qt.Key.Key_Escape:
            self.hide()
            return True
        if obj is self.q and key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self.list.count():
            # Into the list, so the focus is on the command (a screen reader
            # reads each one as it is reached; selection alone is not announced).
            r = self.list.currentRow() + (1 if key == Qt.Key.Key_Down else -1)
            self.list.setCurrentRow(max(0, min(self.list.count() - 1, r)))
            self.list.setFocus()
            return True
        if obj is self.list:
            if key == Qt.Key.Key_Up and self.list.currentRow() <= 0:
                self.q.setFocus()
                return True
            # Typing in the list goes on filtering in the search field.
            if key == Qt.Key.Key_Backspace:
                self.q.setFocus()
                self.q.backspace()
                return True
            t = ev.text()
            if t and t.isprintable() and not ev.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
                self.q.setFocus()
                self.q.insert(t)
                return True
        return super().eventFilter(obj, ev)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
