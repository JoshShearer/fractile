"""Workspace dashboard: snapshots and named layouts, a wireframe preview of
what each one contains, restore/apply with a dry run first, progress."""
from __future__ import annotations

import json

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QSplitter,
                             QTabWidget, QVBoxLayout, QWidget)

from .theme import accent, is_dark

MODE_LABEL = {"manual": "Saved by you", "periodic": "Automatic", "logout": "At logout", "shutdown": "At shutdown", "pre_restore": "Before a restore", "layout": "Layout", "topology": "Monitor setup"}


class Wireframe(QWidget):
    """Monitors and window rectangles, scaled to fit."""

    def __init__(self):
        super().__init__()
        self.outputs: list[dict] = []
        self.boxes: list[tuple[dict, str, bool]] = []  # rect, label, missing
        self.setMinimumSize(360, 220)
        self.setAccessibleName("Layout preview")

    def set_data(self, outputs, boxes):
        self.outputs, self.boxes = outputs or [], boxes or []
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.outputs:
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Nothing to preview")
            return
        xs = [o["rect"]["x"] for o in self.outputs] + [o["rect"]["x"] + o["rect"]["w"] for o in self.outputs]
        ys = [o["rect"]["y"] for o in self.outputs] + [o["rect"]["y"] + o["rect"]["h"] for o in self.outputs]
        minx, miny, maxx, maxy = min(xs), min(ys), max(xs), max(ys)
        pad = 12
        sc = min((self.width() - 2 * pad) / max(1, maxx - minx), (self.height() - 2 * pad) / max(1, maxy - miny))
        def m(r):
            return QRectF(pad + (r["x"] - minx) * sc, pad + (r["y"] - miny) * sc, r["w"] * sc, r["h"] * sc)
        dark = is_dark(self)
        for o in self.outputs:
            p.setPen(QPen(QColor(255, 255, 255, 90) if dark else QColor(0, 0, 0, 90), 1.5))
            p.setBrush(QColor(255, 255, 255, 12) if dark else QColor(0, 0, 0, 10))
            p.drawRoundedRect(m(o["rect"]), 6, 6)
            p.drawText(m(o["rect"]).adjusted(6, 4, -6, -4), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, o.get("description") or o.get("name") or "")
        hl = accent(self)
        base = self.palette().color(self.backgroundRole())
        # Opaque fills (accent blended over the background) so a window that
        # overlaps another hides its label instead of printing over it.
        def blend(a: float) -> QColor:
            return QColor(int(base.red() * (1 - a) + hl.red() * a), int(base.green() * (1 - a) + hl.green() * a), int(base.blue() * (1 - a) + hl.blue() * a))
        for r, label, missing in self.boxes:
            rr = m(r).adjusted(1, 1, -1, -1)
            p.setBrush(blend(0.25 if missing else 0.55))
            pen = QPen(hl, 1.5)
            if missing:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawRoundedRect(rr, 4, 4)
            p.save()
            p.setClipRect(rr)
            p.setPen(QColor(255, 255, 255) if dark else QColor(0, 0, 0))
            p.drawText(rr.adjusted(4, 2, -4, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, label)
            p.restore()


class DashboardWindow(QWidget):
    def __init__(self, app):
        super().__init__(None, Qt.WindowType.Window)
        self.app = app
        self.setWindowTitle("Fractile Workspaces")
        self.resize(980, 600)
        v = QVBoxLayout(self)
        split = QSplitter()
        v.addWidget(split, 1)
        left = QTabWidget()
        self.snaps = QListWidget()
        self.snaps.currentItemChanged.connect(lambda *_: self.show_snapshot())
        self.layouts = QListWidget()
        self.layouts.currentItemChanged.connect(lambda *_: self.show_layout())
        left.addTab(self.snaps, "Snapshots")
        left.addTab(self.layouts, "Layouts")
        left.currentChanged.connect(lambda i: self.show_snapshot() if i == 0 else self.show_layout())
        self.left = left
        split.addWidget(left)
        right = QWidget()
        rv = QVBoxLayout(right)
        self.title = QLabel()
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.25)
        f.setBold(True)
        self.title.setFont(f)
        rv.addWidget(self.title)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        rv.addWidget(self.detail)
        self.wire = Wireframe()
        rv.addWidget(self.wire, 1)
        self.actions = QHBoxLayout()
        rv.addLayout(self.actions)
        split.addWidget(right)
        split.setSizes([320, 660])
        bottom = QHBoxLayout()
        self.prog = QProgressBar()
        self.prog.setVisible(False)
        self.prog_lbl = QLabel()
        self.cancel_b = QPushButton("Cancel restore")
        self.cancel_b.setVisible(False)
        self.cancel_b.clicked.connect(lambda: self.app.bridge.try_call("restore_cancel"))
        bottom.addWidget(self.prog_lbl, 1)
        bottom.addWidget(self.prog, 2)
        bottom.addWidget(self.cancel_b)
        for text, fn in (("Save workspace now", self.save_now), ("Save windows as layout…", self.save_layout), ("Import…", self.import_any), ("Undo last restore", self.undo_restore)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bottom.addWidget(b)
        v.addLayout(bottom)

    # ---- data ----
    def reload(self):
        cur_s = self.snaps.currentItem().data(Qt.ItemDataRole.UserRole) if self.snaps.currentItem() else None
        cur_l = self.layouts.currentItem().data(Qt.ItemDataRole.UserRole) if self.layouts.currentItem() else None
        self.snaps.clear()
        for s in self.app.bridge.try_call("snapshot_list", {"limit": 200}) or []:
            when = s["created_at"][:16].replace("T", " ")
            name = s.get("name") or MODE_LABEL.get(s["mode"], s["mode"])
            flag = ("Pinned \u00b7 " if s.get("pinned") else "") + ("⚠ damaged  " if not s.get("checksum_ok", True) else "")
            it = QListWidgetItem(f"{flag}{name}\n{when}  ·  {s['window_count']} windows, {s['app_count']} apps")
            it.setData(Qt.ItemDataRole.UserRole, s["id"])
            it.setData(Qt.ItemDataRole.UserRole + 1, s)
            self.snaps.addItem(it)
            if s["id"] == cur_s:
                self.snaps.setCurrentItem(it)
        self.layouts.clear()
        for l in self.app.bridge.try_call("layout_list") or []:
            n = sum(len(v.get("entries", [])) for v in l.get("variants", []))
            it = QListWidgetItem(f"{'★ ' if l.get('favorite') else ''}{l['name']}\n{n} windows  ·  {l.get('description', '')[:60]}")
            it.setData(Qt.ItemDataRole.UserRole, l["name"])
            self.layouts.addItem(it)
            if l["name"] == cur_l:
                self.layouts.setCurrentItem(it)
        if self.snaps.currentItem() is None and self.snaps.count():
            self.snaps.setCurrentRow(0)
        if self.left.currentIndex() == 0:
            self.show_snapshot()
        else:
            self.show_layout()

    def _set_actions(self, items):
        while self.actions.count():
            w = self.actions.takeAt(0).widget()
            if w:
                w.deleteLater()
        for text, fn in items:
            b = QPushButton(text)
            b.clicked.connect(fn)
            self.actions.addWidget(b)
        self.actions.addStretch(1)

    def show_snapshot(self):
        it = self.snaps.currentItem()
        if not it:
            self.title.setText("No snapshots yet")
            self.detail.setText("Use “Save workspace now”, or choose an automatic restore policy in Settings → Session Restore.")
            self.wire.set_data([], [])
            self._set_actions([])
            return
        sid = it.data(Qt.ItemDataRole.UserRole)
        meta = it.data(Qt.ItemDataRole.UserRole + 1)
        s = self.app.bridge.try_call("snapshot_get", {"id": sid})
        if not s:
            self.title.setText("This snapshot is damaged")
            self.detail.setText("Its checksum does not match, so Fractile will not restore it.")
            self.wire.set_data([], [])
            self._set_actions([("Delete", lambda: self.delete_snapshot(sid))])
            return
        self.title.setText(s.get("name") or MODE_LABEL.get(s["mode"], s["mode"]))
        apps = sorted({(w.get("desktop_file_id") or w.get("wm_class") or "?").removesuffix(".desktop") for w in s["windows"]})
        self.detail.setText(f"{s['created_at'][:19].replace('T', ' ')}  ·  {s['session_type']} / {s['desktop']}\n{len(s['windows'])} windows: " + ", ".join(apps[:12]) + (" …" if len(apps) > 12 else ""))
        boxes = [(w["absolute_geometry"], (w.get("desktop_file_id") or w.get("wm_class") or "?").removesuffix(".desktop"), False) for w in s["windows"] if not w["state"].get("minimized")]
        self.wire.set_data(s["outputs"], boxes)
        self._set_actions([("Restore…", lambda: self.app.show_recovery({"snapshot_id": sid, "reason": "dashboard"})),
                           ("Unpin" if meta.get("pinned") else "Pin", lambda: self.pin(sid, not meta.get("pinned"))),
                           ("Save as layout…", lambda: self.snapshot_to_layout(s)),
                           ("Export…", lambda: self.export("snapshot", sid)), ("Delete", lambda: self.delete_snapshot(sid))])

    def show_layout(self):
        it = self.layouts.currentItem()
        if not it:
            self.title.setText("No layouts yet")
            self.detail.setText("Arrange your windows, then “Save windows as layout…”. Layouts can be applied any time or at login.")
            self.wire.set_data([], [])
            self._set_actions([])
            return
        name = it.data(Qt.ItemDataRole.UserRole)
        pv = self.app.bridge.try_call("layout_preview", {"name": name})
        if not pv:
            self.title.setText(name)
            self.detail.setText("This layout cannot be shown on the current monitors.")
            self._set_actions([("Delete", lambda: self.delete_layout(name))])
            return
        self.title.setText(name)
        missing = pv["diff"]["missing_entries"]
        extra = len(pv["diff"]["unmatched_open_windows"])
        self.detail.setText(f"{len(pv['items'])} windows · {missing} not open now (dashed) · {extra} open windows not in this layout")
        boxes = [(i["rect"], i["entry"].get("label", ""), not i["matched_windows"]) for i in pv["items"] if i.get("rect")]
        self.wire.set_data(pv["outputs"], boxes)
        self._set_actions([("Apply…", lambda: self.app.show_recovery({"layout": name, "reason": "dashboard"})),
                           ("Position open windows only", lambda: self.apply_position_only(name)),
                           ("Favorite", lambda: self.toggle_fav(name)), ("Export…", lambda: self.export("layout", name)), ("Delete", lambda: self.delete_layout(name))])

    # ---- actions ----
    def save_now(self):
        name, ok = QInputDialog.getText(self, "Save workspace", "Name (optional):")
        if not ok:
            return
        self.app.bridge.call_async("snapshot_save", {"name": name.strip() or None}, lambda _r: self.reload(), lambda c, m: QMessageBox.warning(self, "Save", m))

    def save_layout(self):
        name, ok = QInputDialog.getText(self, "Save layout", "Layout name:")
        if not ok or not name.strip():
            return
        launch = QMessageBox.question(self, "Save layout", "Launch missing apps when this layout is applied?\n(You can always choose per restore.)") == QMessageBox.StandardButton.Yes
        try:
            self.app.bridge.call("layout_from_windows", {"name": name.strip(), "launch": launch, "save": True})
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save layout", getattr(e, "message", str(e)))
        self.left.setCurrentIndex(1)
        self.reload()

    def snapshot_to_layout(self, s):
        QMessageBox.information(self, "Save as layout", "Restore this snapshot first, then use “Save windows as layout…”. Layouts are built from real windows so they match reliably.")

    def apply_position_only(self, name):
        self.app.bridge.call_async("layout_apply", {"name": name, "mode": "position_existing", "options": {"launch": False}}, None, lambda c, m: QMessageBox.warning(self, "Apply", m))

    def toggle_fav(self, name):
        l = self.app.bridge.try_call("layout_get", {"name": name})
        if l:
            l["favorite"] = not l.get("favorite")
            self.app.bridge.try_call("layout_save", {"layout": l})
            self.reload()

    def pin(self, sid, on):
        self.app.bridge.try_call("snapshot_pin", {"id": sid, "pinned": on})
        self.reload()

    def delete_snapshot(self, sid):
        if QMessageBox.question(self, "Delete snapshot", "Delete this snapshot?") == QMessageBox.StandardButton.Yes:
            self.app.bridge.try_call("snapshot_delete", {"id": sid})
            self.reload()

    def delete_layout(self, name):
        if QMessageBox.question(self, "Delete layout", f"Delete layout “{name}”?") == QMessageBox.StandardButton.Yes:
            self.app.bridge.try_call("layout_delete", {"name": name})
            self.reload()

    def export(self, kind, ident):
        data = self.app.bridge.try_call("snapshot_get" if kind == "snapshot" else "layout_get", {"id": ident, "redacted": False} if kind == "snapshot" else {"name": ident})
        if not data:
            return
        if kind == "snapshot" and QMessageBox.question(self, "Export", "Hide window titles in the exported file?") == QMessageBox.StandardButton.Yes:
            data = self.app.bridge.try_call("snapshot_get", {"id": ident, "redacted": True})
        fn, _ = QFileDialog.getSaveFileName(self, "Export", f"fractile-{kind}.json", "JSON (*.json)")
        if fn:
            with open(fn, "w") as f:
                json.dump(data, f, indent=2)

    def import_any(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Import snapshot or layout", "", "JSON (*.json)")
        if not fn:
            return
        try:
            with open(fn) as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Import", f"Not valid JSON: {e}")
            return
        try:
            if isinstance(data, dict) and "snapshot_id" in data:
                self.app.bridge.call("snapshot_import", {"snapshot": data})
            elif isinstance(data, dict) and "variants" in data:
                self.app.bridge.call("layout_save", {"layout": data, "import": True})
            else:
                raise ValueError("this file is neither a Fractile snapshot nor a layout")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Import", getattr(e, "message", str(e)))
            return
        QMessageBox.information(self, "Import", "Imported. Apps from imported files are never launched without asking.")
        self.reload()

    def undo_restore(self):
        self.app.bridge.call_async("restore_undo", {}, lambda _r: None, lambda c, m: QMessageBox.information(self, "Undo restore", m))

    def on_restore_event(self, ev, data):
        if ev == "restore_started":
            self.prog.setVisible(True)
            self.cancel_b.setVisible(True)
            self.prog.setRange(0, len(data.get("plan", {}).get("items") or []) or 1)
            self.prog.setValue(0)
            self.prog_lbl.setText("Restoring…")
        elif ev == "restore_progress":
            self.prog.setRange(0, max(1, data.get("total", 1)))
            self.prog.setValue(data.get("done", 0))
            it = data.get("item") or {}
            self.prog_lbl.setText(f"{data.get('phase', '')}: {it.get('label', '')} {it.get('status', '')}".strip(": "))
        elif ev == "restore_finished":
            self.prog.setVisible(False)
            self.cancel_b.setVisible(False)
            rep = data.get("report") or {}
            items = rep.get("items") or []
            ok = sum(1 for i in items if i.get("status") in ("ALREADY_PRESENT", "LAUNCHED_AND_PLACED", "PLACED_ONLY"))
            self.prog_lbl.setText(f"Restore {data.get('state')}: {ok} of {len(items)} windows restored in {rep.get('duration_ms', 0) / 1000:.1f} s")
            self.reload()
