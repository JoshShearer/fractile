"""Recovery dialog: shows the dry-run plan first, lets the user pick what to
restore and approve launches per app, then runs it with progress."""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout)

from .theme import mute

ACTION_TEXT = {
    "already_open": "Move into place (already open)",
    "will_reopen": "Open, then move into place",
    "needs_approval": "Not running: tick to open it",
    "not_found": "Cannot reopen",
    "cannot_restore_geometry": "This desktop does not allow moving windows",
    "excluded": "Excluded",
    "already_done": "Already restored",
}
SELECTABLE = {"already_open", "will_reopen", "needs_approval"}
DEFAULT_ON = {"already_open", "will_reopen"}


class _EnterPressesDefault(QObject):
    """Enter or Return on a row's checkbox presses the dialog's default
    button. Some Qt styles toggle a focused checkbox on Enter (seen with the
    Ubuntu 24.04 GNOME Wayland session), so a user pressing Enter to accept
    the restore silently unticked the first window instead. Space still
    toggles."""

    def __init__(self, button: QPushButton):
        super().__init__(button)
        self.button = button

    def eventFilter(self, obj, ev):  # noqa: N802 (Qt API)
        if ev.type() == QEvent.Type.KeyPress and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.button.isEnabled():
                self.button.click()
            return True
        return False


class RecoveryDialog(QDialog):
    def __init__(self, app, data: dict):
        # A top-level window, not a dialog: a parentless dialog is transient for
        # the whole client group on X11 and the WM would stack it above the
        # placement panel.
        super().__init__(None, Qt.WindowType.Window)
        self.app = app
        self.data = data or {}
        self.layout_name = self.data.get("layout")
        self.snapshot = self.data.get("snapshot_id") or "latest"
        reason = self.data.get("reason", "")
        self.setWindowTitle("Restore windows")
        self.resize(760, 520)
        v = QVBoxLayout(self)
        head = QLabel()
        head.setWordWrap(True)
        f = head.font()
        f.setPointSizeF(f.pointSizeF() * 1.2)
        f.setBold(True)
        head.setFont(f)
        if reason == "unclean_shutdown" or self.data.get("unclean"):
            head.setText("Your last session ended unexpectedly")
        elif reason in ("ask_each_login", "confirm"):
            head.setText("Restore your windows from last time?")
        elif self.layout_name:
            head.setText(f"Apply layout “{self.layout_name}”")
        else:
            head.setText("Restore windows")
        v.addWidget(head)
        self.sub = QLabel("Checking what can be restored…")
        self.sub.setWordWrap(True)
        v.addWidget(self.sub)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Application", "What will happen", "Content"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        v.addWidget(self.table, 1)
        self.notes = QLabel()
        self.notes.setWordWrap(True)
        mute(self.notes)
        v.addWidget(self.notes)
        self.prog = QProgressBar()
        self.prog.setVisible(False)
        v.addWidget(self.prog)
        row = QHBoxLayout()
        self.never = QPushButton("Don't restore automatically")
        self.never.setToolTip("Switch to manual restore. You can change this in Settings → Session Restore.")
        self.never.clicked.connect(self.disable_auto)
        self.never.setVisible(reason in ("ask_each_login", "confirm", "unclean_shutdown"))
        row.addWidget(self.never)
        row.addStretch(1)
        self.later = QPushButton("Not now")
        self.later.clicked.connect(self.reject)
        row.addWidget(self.later)
        self.go = QPushButton("Restore selected")
        self.go.setDefault(True)
        self.go.setEnabled(False)
        self.go.clicked.connect(self.run)
        self._enter = _EnterPressesDefault(self.go)
        row.addWidget(self.go)
        v.addLayout(row)
        self.run_id = None
        self.load_plan()

    def _params(self) -> dict:
        p = {"options": {"launch": True}}
        if self.layout_name:
            p["layout"] = self.layout_name
            p["mode"] = "launch_missing"
        else:
            p["snapshot"] = self.snapshot
        return p

    def load_plan(self):
        self.app.bridge.call_async("restore_plan", self._params(), self.show_plan, lambda c, m: self.sub.setText(f"Nothing to restore: {m}"))

    def show_plan(self, plan: dict):
        self.plan = plan
        s = plan.get("snapshot")
        if s:
            self.sub.setText(f"From {s.get('created_at', '')[:16].replace('T', ' ')} · {s.get('windows')} windows. Untick anything you don't want. Apps that are not running are only opened when ticked.")
        else:
            self.sub.setText("Untick anything you don't want. Apps that are not running are only opened when ticked.")
        t = self.table
        t.setRowCount(0)
        self.checks: list[tuple[QCheckBox, dict]] = []
        for it in plan.get("items", []):
            r = t.rowCount()
            t.insertRow(r)
            cb = QCheckBox()
            act = it["action"]
            cb.setEnabled(act in SELECTABLE)
            cb.setChecked(act in DEFAULT_ON)
            cb.setAccessibleName(f"Restore {it['label']}")
            cb.installEventFilter(self._enter)
            t.setCellWidget(r, 0, cb)
            t.setItem(r, 1, QTableWidgetItem(it["label"]))
            what = ACTION_TEXT.get(act, act)
            if act == "not_found" and it.get("reason"):
                what += f" ({it['reason']})"
            if it.get("launch_method") and act in ("will_reopen", "needs_approval"):
                pass
            t.setItem(r, 2, QTableWidgetItem(what))
            content = "App restores its documents" if it.get("restore_content") == "application_managed" else "Window only"
            ci = QTableWidgetItem(content)
            ci.setToolTip("The app's own session restore brings back its tabs/documents." if content.startswith("App") else "Position and size only. Unsaved work in this app is not restored.")
            t.setItem(r, 3, ci)
            self.checks.append((cb, it))
        t.resizeColumnsToContents()
        notes = [plan.get("limitations") or "Fractile restores windows, not unsaved document contents."]
        for w in plan.get("warnings", []):
            notes.append("⚠ " + w)
        remapped = [f"{m.get('recorded_id')} \u2192 {m.get('present_id')}" for m in plan.get("output_mapping") or []
                    if isinstance(m, dict) and (not m.get("exact") or m.get("recorded_id") != m.get("present_id"))]
        if remapped:
            notes.append("Monitors changed: windows will go to " + "; ".join(remapped))
        self.notes.setText("\n".join(notes))
        self.go.setEnabled(any(cb.isEnabled() for cb, _ in self.checks))
        # Start on the default action: Enter restores, Tab reaches the rows.
        if self.go.isEnabled():
            self.go.setFocus(Qt.FocusReason.OtherFocusReason)

    def run(self):
        selected = [it["record_id"] for cb, it in self.checks if cb.isChecked() and cb.isEnabled()]
        approved = sorted({it["app"] for cb, it in self.checks if cb.isChecked() and it["action"] == "needs_approval" and it.get("app")})
        p = self._params()
        p["options"].update(selected=selected, approved=approved)
        self.go.setEnabled(False)
        self.later.setText("Close")
        self.prog.setVisible(True)
        self.prog.setRange(0, max(1, len(selected)))
        self.app.bridge.call_async("layout_apply" if self.layout_name else "restore_start", ({"name": self.layout_name, **p} if self.layout_name else p),
                                   lambda r: setattr(self, "run_id", (r or {}).get("run_id")), lambda c, m: self.sub.setText("Could not start: " + m))

    def on_restore_event(self, ev: str, data: dict):
        if self.run_id and data.get("run_id") and data["run_id"] != self.run_id:
            return
        if ev == "restore_progress":
            self.prog.setRange(0, max(1, data.get("total", 1)))
            self.prog.setValue(data.get("done", 0))
            it = data.get("item") or {}
            if it:
                self.sub.setText(f"{it.get('label', '')}: {it.get('status', '').replace('_', ' ').lower()}")
        elif ev == "restore_finished":
            rep = data.get("report") or {}
            items = rep.get("items") or []
            ok = [i for i in items if i.get("status") in ("ALREADY_PRESENT", "LAUNCHED_AND_PLACED", "PLACED_ONLY")]
            bad = [i for i in items if i not in ok and not i.get("excluded")]
            self.prog.setValue(self.prog.maximum())
            lines = [f"Done: {len(ok)} of {len(items)} windows restored."]
            for i in bad[:8]:
                lines.append(f"• {i['label']}: {i['status'].replace('_', ' ').lower()}" + (f" ({i['message']})" if i.get("message") else ""))
            self.sub.setText("\n".join(lines))
            self.later.setText("Close")

    def disable_auto(self):
        self.app.bridge.try_call("set_config", {"patch": {"session": {"policy": "manual"}}})
        self.reject()
