"""Settings (DV-25): General, Appearance, Shortcuts, Session Restore, Rules,
Monitors, Diagnostics, About. Edits a working copy; Apply validates in the
service and saves atomically. Nothing changes until Apply."""
from __future__ import annotations

import copy
import json
import uuid

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
                             QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from . import __version__
from .theme import mute
from .widgets import GridPicker, ShortcutEdit

SNAP_ACTIONS = ["left-half", "right-half", "top-half", "bottom-half", "left-third", "center-third", "right-third", "left-two-thirds", "right-two-thirds",
                "top-left-quarter", "top-right-quarter", "bottom-left-quarter", "bottom-right-quarter", "center", "maximize"]
ACTION_TYPES = [("place", "Place on grid"), ("snap", "Snap"), ("move_to_next_output", "Move to next monitor"), ("move_to_prev_output", "Move to previous monitor"),
                ("move_to_workspace", "Move to workspace"), ("undo", "Undo last placement"), ("save_workspace", "Save workspace"), ("restore_latest", "Restore latest snapshot"),
                ("apply_layout", "Apply layout"), ("toggle_panel", "Show/hide panel"), ("command_palette", "Command palette")]
POLICIES = [("off", "Off: never capture or restore"), ("manual", "Manual: save and restore only when I ask"), ("ask", "Ask at each login"),
            ("last_clean", "Restore the last clean session automatically"), ("last_known", "Restore the last known session automatically"), ("named", "Apply a named layout at login")]
LAUNCH = [("never", "Never launch"), ("ask", "Ask"), ("if_missing", "Launch if missing"), ("always", "Always launch")]
OUTPUT_KINDS = [("current", "Window's current monitor"), ("primary", "Primary monitor"), ("largest", "Largest monitor"), ("index", "Monitor number"), ("stable", "Specific monitor")]


def action_text(a: dict) -> str:
    t = a.get("type")
    if t == "place":
        g, s = a["grid"], a["selection"]
        return f"Grid {g['cols']}×{g['rows']} cells {s['col0']},{s['row0']}–{s['col1']},{s['row1']}"
    if t == "snap":
        return "Snap " + a.get("action", "")
    if t == "move_to_workspace":
        return f"Move to workspace {a.get('index', 0) + 1}"
    if t == "apply_layout":
        return f"Apply layout “{a.get('name', '')}”"
    return dict(ACTION_TYPES).get(t, t)


def getp(d: dict, path: str, default=None):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def setp(d: dict, path: str, value):
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


PANEL_ROW = "__panel__"  # table row for general.panel_shortcut


class Binder:
    def __init__(self, win: "SettingsWindow"):
        self.win = win
        self.loaders = []

    def load(self):
        for f in self.loaders:
            f()

    def check(self, path: str, text: str, tip: str = "") -> QCheckBox:
        w = QCheckBox(text)
        if tip:
            w.setToolTip(tip)
        self.loaders.append(lambda: w.setChecked(bool(getp(self.win.cfg, path, False))))
        w.toggled.connect(lambda v: self.win.edit(path, v))
        return w

    def spin(self, path: str, lo: int, hi: int, suffix: str = "") -> QSpinBox:
        w = QSpinBox()
        w.setRange(lo, hi)
        if suffix:
            w.setSuffix(suffix)
        self.loaders.append(lambda: w.setValue(int(getp(self.win.cfg, path, lo) or lo)))
        w.valueChanged.connect(lambda v: self.win.edit(path, v))
        return w

    def dspin(self, path: str, lo: float, hi: float, step: float = 0.1) -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        self.loaders.append(lambda: w.setValue(float(getp(self.win.cfg, path, lo) or lo)))
        w.valueChanged.connect(lambda v: self.win.edit(path, round(v, 3)))
        return w

    def combo(self, path: str, options: list[tuple[str, str]]) -> QComboBox:
        w = QComboBox()
        for v, label in options:
            w.addItem(label, v)
        def load():
            i = w.findData(getp(self.win.cfg, path))
            w.setCurrentIndex(max(0, i))
        self.loaders.append(load)
        w.currentIndexChanged.connect(lambda _i: self.win.edit(path, w.currentData()))
        return w

    def length(self, path: str) -> QWidget:
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        val = QDoubleSpinBox()
        val.setRange(0, 400)
        unit = QComboBox()
        unit.addItem("px", "px")
        unit.addItem("%", "percent")
        h.addWidget(val)
        h.addWidget(unit)
        def load():
            l = getp(self.win.cfg, path) or {"unit": "px", "value": 0}
            unit.blockSignals(True)
            val.blockSignals(True)
            unit.setCurrentIndex(0 if l["unit"] == "px" else 1)
            val.setDecimals(0 if l["unit"] == "px" else 1)
            val.setRange(0, 400 if l["unit"] == "px" else 45)
            val.setValue(float(l["value"]))
            unit.blockSignals(False)
            val.blockSignals(False)
        def store():
            u = unit.currentData()
            val.setDecimals(0 if u == "px" else 1)
            val.setRange(0, 400 if u == "px" else 45)
            self.win.edit(path, {"unit": u, "value": int(val.value()) if u == "px" else round(val.value(), 2)})
        self.loaders.append(load)
        val.valueChanged.connect(lambda _v: store())
        unit.currentIndexChanged.connect(lambda _i: store())
        return box


class ShortcutDialog(QDialog):
    def __init__(self, app, sc: dict | None, layouts: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit shortcut" if sc else "New shortcut")
        self.app = app
        self.sc = copy.deepcopy(sc) if sc else {"id": "s-" + uuid.uuid4().hex[:8], "name": "", "scope": "global", "accelerator": None, "cycle_monitors": False, "favorite": False,
                                               "action": {"type": "place", "grid": {"cols": 6, "rows": 6}, "selection": {"col0": 0, "row0": 0, "col1": 2, "row1": 5}}}
        f = QFormLayout(self)
        self.name = QLineEdit(self.sc["name"])
        self.name.setPlaceholderText("e.g. Editor on the left")
        f.addRow("Name", self.name)
        self.scope = QComboBox()
        self.scope.addItem("Global: works anywhere, panel closed", "global")
        self.scope.addItem("Panel: only while the panel is open", "local")
        self.scope.setCurrentIndex(0 if self.sc["scope"] == "global" else 1)
        f.addRow("Scope", self.scope)
        self.keys = ShortcutEdit(app, self.sc["scope"], self.sc.get("accelerator"), self.sc["id"])
        f.addRow("Keys", self.keys)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self.atype = QComboBox()
        for v, label in ACTION_TYPES:
            self.atype.addItem(label, v)
        self.atype.setCurrentIndex(max(0, self.atype.findData(self.sc["action"]["type"])))
        f.addRow("Action", self.atype)
        a = self.sc["action"]
        self.picker = GridPicker(a.get("grid"), a.get("selection"))
        f.addRow("Grid area", self.picker)
        self.out = QComboBox()
        for v, label in OUTPUT_KINDS[:3]:
            self.out.addItem(label, v)
        self.out.setCurrentIndex(max(0, self.out.findData((a.get("output") or {}).get("kind", "current"))))
        f.addRow("Monitor", self.out)
        self.snap = QComboBox()
        self.snap.addItems(SNAP_ACTIONS)
        if a.get("action") in SNAP_ACTIONS:
            self.snap.setCurrentText(a["action"])
        f.addRow("Snap to", self.snap)
        self.ws = QSpinBox()
        self.ws.setRange(1, 36)
        self.ws.setValue(int(a.get("index", 0)) + 1)
        f.addRow("Workspace", self.ws)
        self.layout_c = QComboBox()
        self.layout_c.addItems(layouts)
        if a.get("name"):
            self.layout_c.setCurrentText(a["name"])
        f.addRow("Layout", self.layout_c)
        self.cycle = QCheckBox("Pressing again moves the window to the next monitor")
        self.cycle.setChecked(bool(self.sc.get("cycle_monitors")))
        f.addRow("", self.cycle)
        self.fav = QCheckBox("Show as a button on the panel")
        self.fav.setChecked(bool(self.sc.get("favorite")))
        f.addRow("", self.fav)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.form = f
        self.atype.currentIndexChanged.connect(self._vis)
        self._vis()

    def _scope_changed(self):
        self.keys.scope = self.scope.currentData()
        self.keys._render()

    def _vis(self):
        t = self.atype.currentData()
        for w, show in ((self.picker, t == "place"), (self.out, t == "place"), (self.snap, t == "snap"), (self.ws, t == "move_to_workspace"), (self.layout_c, t == "apply_layout"), (self.cycle, t == "place")):
            w.setVisible(show)
            lbl = self.form.labelForField(w)
            if lbl:
                lbl.setVisible(show)

    def _ok(self):
        if not self.name.text().strip():
            QMessageBox.warning(self, "Shortcut", "Give the shortcut a name.")
            return
        t = self.atype.currentData()
        if t == "place" and self.picker.selection() is None:
            QMessageBox.warning(self, "Shortcut", "Drag across the grid to choose the area.")
            return
        if not self.keys.is_ok() and self.keys.value:
            if QMessageBox.question(self, "Shortcut", self.keys.info.text() + "\n\nSave anyway?") != QMessageBox.StandardButton.Yes:
                return
        action: dict = {"type": t}
        if t == "place":
            action.update(grid=self.picker.grid(), selection=self.picker.selection(), output={"kind": self.out.currentData()})
        elif t == "snap":
            action["action"] = self.snap.currentText()
        elif t == "move_to_workspace":
            action["index"] = self.ws.value() - 1
        elif t == "apply_layout":
            if not self.layout_c.currentText():
                QMessageBox.warning(self, "Shortcut", "Save a layout first.")
                return
            action["name"] = self.layout_c.currentText()
        self.sc.update(name=self.name.text().strip(), scope=self.scope.currentData(), accelerator=self.keys.value, action=action,
                       cycle_monitors=self.cycle.isChecked() and t == "place", favorite=self.fav.isChecked())
        self.accept()


class RuleDialog(QDialog):
    def __init__(self, app, rule: dict | None, allow_regex: bool, windows: list[dict], parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Edit rule" if rule else "New rule")
        self.rule = copy.deepcopy(rule) if rule else {"id": "r-" + uuid.uuid4().hex[:8], "name": "", "enabled": True, "trigger": "on_create", "matcher": {},
                                                      "placement": {"kind": "grid", "grid": {"cols": 2, "rows": 1}, "selection": {"col0": 0, "row0": 0, "col1": 0, "row1": 0}},
                                                      "output": {"kind": "current"}}
        f = QFormLayout(self)
        self.name = QLineEdit(self.rule["name"])
        f.addRow("Name", self.name)
        self.from_win = QComboBox()
        self.from_win.addItem("Fill from an open window…", None)
        for w in windows:
            self.from_win.addItem(f"{w.get('desktop_file_id') or w.get('wm_class') or '?'}: {(w.get('title') or '')[:40]}", w)
        self.from_win.currentIndexChanged.connect(self._fill)
        f.addRow("", self.from_win)
        m = self.rule["matcher"]
        self.desk = QLineEdit(m.get("desktop_id") or "")
        self.desk.setPlaceholderText("e.g. org.gnome.TextEditor.desktop")
        f.addRow("Application (desktop id)", self.desk)
        self.wmc = QLineEdit(m.get("wm_class") or "")
        f.addRow("Window class", self.wmc)
        self.appid = QLineEdit(m.get("app_id") or "")
        f.addRow("App id", self.appid)
        self.role = QLineEdit(m.get("role") or "")
        f.addRow("Window role", self.role)
        self.title = QLineEdit(m.get("title_regex") or "")
        self.title.setEnabled(allow_regex)
        self.title.setPlaceholderText("regular expression" if allow_regex else "enable title matching in the Rules tab first")
        f.addRow("Title matches", self.title)
        self.trigger = QComboBox()
        self.trigger.addItem("When a matching window opens", "on_create")
        self.trigger.addItem("Only when I run rules manually", "manual")
        self.trigger.setCurrentIndex(0 if self.rule.get("trigger") == "on_create" else 1)
        f.addRow("Apply", self.trigger)
        p = self.rule.get("placement") or {}
        self.picker = GridPicker(p.get("grid"), p.get("selection"))
        f.addRow("Place in", self.picker)
        self.out = QComboBox()
        for v, label in OUTPUT_KINDS[:3]:
            self.out.addItem(label, v)
        self.out.setCurrentIndex(max(0, self.out.findData((self.rule.get("output") or {}).get("kind"))))
        f.addRow("Monitor", self.out)
        self.test_lbl = QLabel()
        self.test_lbl.setWordWrap(True)
        test = QPushButton("Test against open windows")
        test.clicked.connect(self._test)
        f.addRow(test, self.test_lbl)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _fill(self):
        w = self.from_win.currentData()
        if not w:
            return
        self.desk.setText(w.get("desktop_file_id") or "")
        self.wmc.setText("" if w.get("desktop_file_id") else (w.get("wm_class") or ""))
        self.appid.setText("" if w.get("desktop_file_id") else (w.get("app_id") or ""))
        self.role.setText(w.get("role") or "")
        if not self.name.text():
            self.name.setText((w.get("desktop_file_id") or w.get("wm_class") or "Rule").removesuffix(".desktop"))

    def _collect(self) -> dict:
        m = {k: v for k, v in (("desktop_id", self.desk.text().strip()), ("wm_class", self.wmc.text().strip()), ("app_id", self.appid.text().strip()),
                               ("role", self.role.text().strip()), ("title_regex", self.title.text().strip())) if v}
        r = dict(self.rule)
        r.update(name=self.name.text().strip(), matcher=m, trigger=self.trigger.currentData(), output={"kind": self.out.currentData()})
        sel = self.picker.selection()
        r["placement"] = {"kind": "grid", "grid": self.picker.grid(), "selection": sel} if sel else None
        return r

    def _test(self):
        try:
            res = self.app.bridge.call("rules_test", {"rule": self._collect()})
        except Exception as e:  # noqa: BLE001
            self.test_lbl.setText(f"⚠ {getattr(e, 'message', e)}")
            return
        hits = [x for x in res if x["explanation"]["matched"]]
        lines = [f"{len(hits)} of {len(res)} open windows match."]
        for x in hits[:6]:
            lines.append(f"✓ {x['app']}")
        if not hits and res:
            ex = res[0]["explanation"]
            fails = [c for c in ex["checks"] if not c["ok"]][:3]
            lines.append("Closest: " + ", ".join(f"{c['field']} is “{c['actual']}”, rule wants “{c['expected']}”" for c in fails))
        self.test_lbl.setText("\n".join(lines))

    def _ok(self):
        r = self._collect()
        if not r["name"]:
            QMessageBox.warning(self, "Rule", "Give the rule a name.")
            return
        if not r["matcher"]:
            QMessageBox.warning(self, "Rule", "Fill in at least one matching field.")
            return
        self.rule = r
        self.accept()


class SettingsWindow(QWidget):
    PAGES = ["general", "appearance", "shortcuts", "session", "rules", "monitors", "diagnostics", "about"]

    def __init__(self, app):
        super().__init__(None, Qt.WindowType.Window)
        self.app = app
        self.setWindowTitle("Fractile Settings")
        self.resize(820, 640)
        self.cfg: dict = {}
        self.saved: dict = {}
        self.b = Binder(self)
        self._loading = False
        v = QVBoxLayout(self)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.banner.setStyleSheet("padding: 8px; border-radius: 6px; background: palette(alternate-base);")
        v.addWidget(self.banner)
        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)
        self.tabs.addTab(self._general(), "General")
        self.tabs.addTab(self._appearance(), "Appearance")
        self.tabs.addTab(self._shortcuts(), "Shortcuts")
        self.tabs.addTab(self._session(), "Session Restore")
        self.tabs.addTab(self._rules(), "Rules")
        self.tabs.addTab(self._monitors(), "Monitors")
        self.tabs.addTab(self._diagnostics(), "Diagnostics")
        self.tabs.addTab(self._about(), "About")
        self.tabs.currentChanged.connect(self._tab_changed)
        row = QHBoxLayout()
        self.dirty_lbl = QLabel()
        row.addWidget(self.dirty_lbl, 1)
        self.reset_b = QPushButton("Restore defaults for this page")
        self.reset_b.clicked.connect(self.reset_page)
        row.addWidget(self.reset_b)
        self.revert_b = QPushButton("Revert")
        self.revert_b.clicked.connect(self.reload_from_service)
        row.addWidget(self.revert_b)
        self.apply_b = QPushButton("Apply")
        self.apply_b.setDefault(True)
        self.apply_b.clicked.connect(self.apply)
        row.addWidget(self.apply_b)
        v.addLayout(row)
        self.reload_from_service()

    # ---- model ----
    def reload_from_service(self):
        r = self.app.bridge.try_call("get_config")
        if r is None:
            self.banner.setText("The Fractile service is not running. Settings cannot be changed until it starts.")
            self.banner.setVisible(True)
            return
        self.cfg = r["config"]
        self.saved = copy.deepcopy(self.cfg)
        if r.get("error"):
            self.banner.setText("Your settings file could not be read, so defaults are in use and the file was left unchanged. Applying will replace it.\n" + r["error"][:300])
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)
        self._loading = True
        self.b.load()
        self._load_tables()
        self._loading = False
        self._update_dirty()

    def edit(self, path: str, value):
        if self._loading:
            return
        setp(self.cfg, path, value)
        self._update_dirty()

    def _update_dirty(self):
        dirty = self.cfg != self.saved
        self.apply_b.setEnabled(dirty)
        self.revert_b.setEnabled(dirty)
        self.dirty_lbl.setText("Unsaved changes" if dirty else "")

    def apply(self):
        autostart_changed = getp(self.cfg, "general.autostart") != getp(self.saved, "general.autostart")
        try:
            r = self.app.bridge.call("set_config", {"config": self.cfg})
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Settings not saved", getattr(e, "message", str(e)))
            return
        self.saved = copy.deepcopy(self.cfg)
        self._update_dirty()
        errs = (r or {}).get("hotkey_errors") or {}
        if errs:
            QMessageBox.information(self, "Some shortcuts are not active", "\n".join(f"{k}: {v}" for k, v in errs.items()))
        if autostart_changed:
            self._load_autostart()
        self.app.reload_config()

    def reset_page(self):
        page = self.PAGES[self.tabs.currentIndex()]
        if page not in self.RESETTABLE:
            return
        if QMessageBox.question(self, "Restore defaults", f"Reset the {page} settings to their defaults? This is saved immediately.") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.app.bridge.call("reset_config", {"section": page})
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Restore defaults", getattr(e, "message", str(e)))
        self.reload_from_service()

    def open_page(self, page: str | None):
        if page in self.PAGES:
            self.tabs.setCurrentIndex(self.PAGES.index(page))

    RESETTABLE = ("general", "appearance", "shortcuts", "session", "rules")

    def _tab_changed(self, i: int):
        if hasattr(self, "reset_b"):
            self.reset_b.setVisible(self.PAGES[i] in self.RESETTABLE)
        if self.PAGES[i] == "diagnostics":
            self._refresh_diag()

    # ---- pages ----
    def _general(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.autostart = QCheckBox("Start Fractile when I log in")
        self.autostart.toggled.connect(lambda v: self.edit("general.autostart", v))
        self.autostart_lbl = QLabel()
        mute(self.autostart_lbl)
        f.addRow(self.autostart)
        f.addRow("", self.autostart_lbl)
        self.b.loaders.append(lambda: self.autostart.setChecked(bool(getp(self.cfg, "general.autostart"))))
        self.b.loaders.append(self._load_autostart)
        f.addRow(self.b.check("general.tray_icon", "Show icon in the system tray (when the desktop provides one)"))
        self.panel_key = ShortcutEdit(self.app, "global", None, "panel")
        self.panel_key.changed.connect(lambda v: self.edit("general.panel_shortcut", v))
        self.b.loaders.append(lambda: (setattr(self.panel_key, "value", getp(self.cfg, "general.panel_shortcut")), self.panel_key._render()))
        f.addRow("Show/hide panel shortcut", self.panel_key)
        f.addRow(self.b.check("general.panel_on_all_monitors", "Show a panel on every monitor"))
        f.addRow(self.b.check("general.dismiss_after_apply", "Close the panel after placing a window (hold Shift to do the opposite)"))
        f.addRow(self.b.check("general.cycle_monitors_on_repeat", "Repeating a shortcut moves the window to the next monitor"))
        f.addRow("Repeat window", self.b.spin("general.cycle_timeout_ms", 300, 10000, " ms"))
        f.addRow(self.b.check("general.respect_tiling_manager", "Warn when an automatic tiling manager is active (Fractile never changes its settings)"))
        f.addRow(self.b.check("general.show_help", "Show the key hints at the bottom of the panel"))
        return w

    def _load_autostart(self):
        st = self.app.bridge.try_call("autostart_status") or {}
        m = st.get("mode")
        if m == "none":
            self.autostart_lbl.setText("Login start is managed outside Fractile in this session (FRACTILE_AUTOSTART_MODE=none); this setting is saved but not applied.")
            return
        how = {"systemd": "systemd user service", "xdg": "desktop autostart entry"}.get(m, "unknown method")
        self.autostart_lbl.setText(f"Currently {'on' if st.get('enabled') else 'off'} (using the {how}).")

    def _appearance(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        g = QGroupBox("Grid")
        f = QFormLayout(g)
        f.addRow("Default columns", self.b.spin("appearance.default_grid.cols", 1, 10))
        f.addRow("Default rows", self.b.spin("appearance.default_grid.rows", 1, 10))
        f.addRow(self.b.check("appearance.reset_grid_on_open", "Reset to the default grid each time the panel opens"))
        f.addRow(self.b.check("appearance.subdivision_enabled", "Hold Alt for a finer grid (twice the cells)"))
        f.addRow(self.b.check("appearance.preview_enabled", "Preview the target area while selecting"))
        f.addRow("Cell size", self.b.spin("appearance.cell_size", 16, 120, " px"))
        v.addWidget(g)
        g = QGroupBox("Spacing")
        f = QFormLayout(g)
        f.addRow(self.b.check("appearance.spacing.margins_enabled", "Margins at the screen edges"))
        for side in ("left", "right", "top", "bottom"):
            f.addRow(f"    {side.title()}", self.b.length(f"appearance.spacing.margins.{side}"))
        f.addRow(self.b.check("appearance.spacing.gaps_enabled", "Gaps between windows"))
        f.addRow("    Horizontal", self.b.length("appearance.spacing.gaps.horizontal"))
        f.addRow("    Vertical", self.b.length("appearance.spacing.gaps.vertical"))
        v.addWidget(g)
        g = QGroupBox("Look")
        f = QFormLayout(g)
        f.addRow("Theme", self.b.combo("appearance.theme", [("system", "Follow system"), ("light", "Light"), ("dark", "Dark"), ("high_contrast", "High contrast")]))
        f.addRow("Text size", self.b.dspin("appearance.font_scale", 0.5, 3.0))
        v.addWidget(g)
        v.addStretch(1)
        return w

    def _shortcuts(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("Global shortcuts work anywhere. Panel shortcuts only work while the panel is open."))
        self.sc_table = QTableWidget(0, 5)
        self.sc_table.setHorizontalHeaderLabels(["Name", "Keys", "Where", "Action", "Status"])
        self.sc_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.sc_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sc_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sc_table.doubleClicked.connect(lambda _i: self._sc_edit())
        v.addWidget(self.sc_table, 1)
        row = QHBoxLayout()
        for text, fn in (("Add…", self._sc_add), ("Edit…", self._sc_edit), ("Duplicate", self._sc_dup), ("Remove", self._sc_del), ("Import…", self._sc_import), ("Export…", self._sc_export)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        return w

    def _load_tables(self):
        errs = (self.app.bridge.try_call("status") or {}).get("hotkey_errors") or {}
        t = self.sc_table
        t.setRowCount(0)
        # The panel shortcut lives under General; show it here too so the
        # list answers "which keys does Fractile use?" (read-only row).
        pk = getp(self.cfg, "general.panel_shortcut")
        t.insertRow(0)
        vals = ["Show the panel", pk or "-", "Global", "Set under General",
                ("⚠ " + errs["panel"]) if "panel" in errs else ("active" if pk else "no keys set")]
        for c, val in enumerate(vals):
            it = QTableWidgetItem(val)
            it.setData(Qt.ItemDataRole.UserRole, PANEL_ROW)
            it.setToolTip("The panel shortcut is set on the General page. Edit… opens it.")
            t.setItem(0, c, it)
        for s in self.cfg.get("shortcuts", []):
            r = t.rowCount()
            t.insertRow(r)
            vals = [s["name"], s.get("accelerator") or "-", "Global" if s["scope"] == "global" else "Panel", action_text(s["action"]),
                    ("⚠ " + errs[s["id"]]) if s["id"] in errs else ("active" if s.get("accelerator") else "no keys set")]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                it.setData(Qt.ItemDataRole.UserRole, s["id"])
                t.setItem(r, c, it)
        t.resizeColumnsToContents()
        self._load_rules()
        self._load_apps()
        self._load_profiles()

    def _sel_shortcut(self) -> int | None:
        r = self.sc_table.currentRow()
        if r < 0:
            return None
        sid = self.sc_table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        return next((i for i, s in enumerate(self.cfg["shortcuts"]) if s["id"] == sid), None)

    def _layout_names(self) -> list[str]:
        return [l["name"] for l in (self.app.bridge.try_call("layout_list") or [])]

    def _sc_add(self):
        d = ShortcutDialog(self.app, None, self._layout_names(), self)
        if d.exec():
            self.cfg["shortcuts"].append(d.sc)
            self._load_tables()
            self._update_dirty()

    def _sc_edit(self):
        r = self.sc_table.currentRow()
        if r >= 0 and self.sc_table.item(r, 0).data(Qt.ItemDataRole.UserRole) == PANEL_ROW:
            self.open_page("general")
            self.panel_key.btn.setFocus()
            return
        i = self._sel_shortcut()
        if i is None:
            return
        d = ShortcutDialog(self.app, self.cfg["shortcuts"][i], self._layout_names(), self)
        if d.exec():
            self.cfg["shortcuts"][i] = d.sc
            self._load_tables()
            self._update_dirty()

    def _sc_dup(self):
        i = self._sel_shortcut()
        if i is None:
            return
        s = copy.deepcopy(self.cfg["shortcuts"][i])
        s["id"] = "s-" + uuid.uuid4().hex[:8]
        s["name"] += " (copy)"
        s["accelerator"] = None
        self.cfg["shortcuts"].append(s)
        self._load_tables()
        self._update_dirty()

    def _sc_del(self):
        i = self._sel_shortcut()
        if i is None:
            return
        del self.cfg["shortcuts"][i]
        self._load_tables()
        self._update_dirty()

    def _sc_export(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Export shortcuts", "fractile-shortcuts.json", "JSON (*.json)")
        if fn:
            with open(fn, "w") as f:
                json.dump({"fractile_shortcuts": 1, "shortcuts": self.cfg.get("shortcuts", [])}, f, indent=2)

    def _sc_import(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Import shortcuts", "", "JSON (*.json)")
        if not fn:
            return
        try:
            with open(fn) as f:
                data = json.load(f)
            items = data["shortcuts"] if isinstance(data, dict) else data
            assert isinstance(items, list) and len(items) <= 500
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Import", f"Not a Fractile shortcuts file: {e}")
            return
        existing = {s["id"] for s in self.cfg["shortcuts"]}
        n = 0
        for s in items:
            if not isinstance(s, dict) or "action" not in s or "name" not in s:
                continue
            s = dict(s)
            if s.get("id") in existing:
                s["id"] = "s-" + uuid.uuid4().hex[:8]
            s.setdefault("scope", "global")
            self.cfg["shortcuts"].append(s)
            n += 1
        self._load_tables()
        self._update_dirty()
        QMessageBox.information(self, "Import", f"Imported {n} shortcuts. Press Apply to check and save them.")

    def _session(self) -> QWidget:
        outer = QWidget()
        v = QVBoxLayout(outer)
        note = QLabel("Fractile restores application windows: which apps were open, and each window's monitor, workspace, size and position. "
                      "It cannot bring back unsaved document contents or in-memory state; apps that have their own session restore (browsers, many editors) reopen their own tabs.")
        note.setWordWrap(True)
        v.addWidget(note)
        tabs = QTabWidget()
        v.addWidget(tabs, 1)
        w = QWidget()
        f = QFormLayout(w)
        pol = self.b.combo("session.policy", POLICIES)
        f.addRow("At login", pol)
        self.named = QComboBox()
        self.named.setEditable(False)
        def load_named():
            self.named.blockSignals(True)
            self.named.clear()
            self.named.addItems(self._layout_names())
            cur = getp(self.cfg, "session.named_layout")
            if cur:
                self.named.setCurrentText(cur)
            self.named.blockSignals(False)
        self.b.loaders.append(load_named)
        self.named.currentTextChanged.connect(lambda t: self.edit("session.named_layout", t or None))
        f.addRow("Layout to apply", self.named)
        f.addRow(self.b.check("session.confirm_before_restore", "Show what will be restored and ask first"))
        f.addRow(self.b.check("session.restore_once_per_login", "Restore at most once per login"))
        f.addRow("After a crash or power loss", self.b.combo("session.power_loss_policy", [("ask", "Ask"), ("restore", "Restore anyway"), ("skip", "Do not restore")]))
        f.addRow(self.b.check("session.restore_on_monitor_reconnect", "Move windows back when a monitor is reconnected"))
        f.addRow(self.b.check("session.preserve_open_windows", "Never close or reuse windows that are already open"))
        tabs.addTab(w, "Restore")
        w = QWidget()
        f = QFormLayout(w)
        f.addRow(self.b.check("session.autosave_enabled", "Save snapshots automatically (only when an automatic policy is chosen)"))
        f.addRow("Every", self.b.spin("session.autosave_interval_min", 1, 1440, " min"))
        f.addRow("Keep recent snapshots", self.b.spin("session.max_recent", 1, 500))
        f.addRow("Keep one per day for", self.b.spin("session.daily_history_days", 0, 365, " days"))
        f.addRow(self.b.check("session.store_titles", "Store window titles (helps tell windows apart; turn off for privacy)"))
        f.addRow(self.b.check("session.ignore_private_windows", "Never save private/incognito windows"))
        self.priv = QPlainTextEdit()
        self.priv.setFixedHeight(80)
        self.b.loaders.append(lambda: self.priv.setPlainText("\n".join(getp(self.cfg, "session.private_title_patterns", []))))
        self.priv.textChanged.connect(lambda: self.edit("session.private_title_patterns", [l for l in self.priv.toPlainText().splitlines() if l.strip()]))
        f.addRow("Private title patterns", self.priv)
        f.addRow(self.b.check("session.skip_capture_when_locked", "Do not capture while the screen is locked"))
        tabs.addTab(w, "Snapshots")
        w = QWidget()
        f = QFormLayout(w)
        f.addRow("Launch at most", self.b.spin("session.max_concurrent_launches", 1, 16, " apps at once"))
        f.addRow("Wait per app", self.b.spin("session.per_app_timeout_s", 1, 600, " s"))
        f.addRow("Give up after", self.b.spin("session.total_timeout_s", 1, 3600, " s"))
        f.addRow("Wait for the desktop", self.b.spin("session.readiness_timeout_s", 5, 600, " s"))
        f.addRow("Pause automatic restore after", self.b.spin("session.max_consecutive_failures", 1, 20, " failures"))
        tabs.addTab(w, "Timing")
        w = QWidget()
        vv = QVBoxLayout(w)
        vv.addWidget(QLabel("How each application may be started during a restore. New apps default to Ask."))
        self.apps_table = QTableWidget(0, 4)
        self.apps_table.setHorizontalHeaderLabels(["Application", "Launch", "Restores its own documents", "Never restore"])
        self.apps_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        vv.addWidget(self.apps_table, 1)
        tabs.addTab(w, "Applications")
        return outer

    def _load_apps(self):
        apps = self.app.bridge.try_call("apps_list") or []
        t = self.apps_table
        t.setRowCount(0)
        dnr = set(getp(self.cfg, "session.do_not_restore", []))
        pol = self.cfg.get("apps", {})
        for a in apps:
            if a.get("terminal"):
                continue
            r = t.rowCount()
            t.insertRow(r)
            it = QTableWidgetItem(a["name"])
            it.setToolTip(a["id"])
            t.setItem(r, 0, it)
            c = QComboBox()
            for v, label in LAUNCH:
                c.addItem(label, v)
            p = pol.get(a["id"]) or {}
            c.setCurrentIndex(max(0, c.findData(p.get("launch", "ask"))))
            c.currentIndexChanged.connect(lambda _i, aid=a["id"], cb=c: self._app_edit(aid, launch=cb.currentData()))
            t.setCellWidget(r, 1, c)
            ms = QCheckBox()
            ms.setChecked(bool(p.get("app_manages_session")))
            ms.toggled.connect(lambda val, aid=a["id"]: self._app_edit(aid, app_manages_session=val))
            t.setCellWidget(r, 2, ms)
            nr = QCheckBox()
            nr.setChecked(a["id"] in dnr)
            nr.toggled.connect(lambda val, aid=a["id"]: self._dnr(aid, val))
            t.setCellWidget(r, 3, nr)

    def _app_edit(self, aid: str, **kw):
        apps = self.cfg.setdefault("apps", {})
        cur = apps.get(aid) or {"launch": "ask", "extra_args": [], "app_manages_session": False}
        cur.update(kw)
        apps[aid] = cur
        self._update_dirty()

    def _dnr(self, aid: str, on: bool):
        lst = list(getp(self.cfg, "session.do_not_restore", []))
        if on and aid not in lst:
            lst.append(aid)
        if not on and aid in lst:
            lst.remove(aid)
        self.edit("session.do_not_restore", lst)

    def _rules(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self.b.check("rules_enabled", "Place matching windows automatically when they open"))
        v.addWidget(self.b.check("allow_title_regex", "Allow matching on window titles (titles change often; less reliable)"))
        self.rules_list = QListWidget()
        self.rules_list.itemDoubleClicked.connect(lambda _i: self._rule_edit())
        v.addWidget(self.rules_list, 1)
        row = QHBoxLayout()
        for text, fn in (("Add…", self._rule_add), ("Edit…", self._rule_edit), ("Remove", self._rule_del), ("Move up", lambda: self._rule_move(-1)), ("Move down", lambda: self._rule_move(1)), ("Apply rules now", self._rule_apply)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(QLabel("The first matching rule wins. Rules never launch applications."))
        return w

    def _load_rules(self):
        self.rules_list.clear()
        for r in self.cfg.get("rules", []):
            m = r.get("matcher", {})
            what = m.get("desktop_id") or m.get("wm_class") or m.get("app_id") or "?"
            it = QListWidgetItem(f"{'✓' if r.get('enabled', True) else '✗'}  {r['name']}  ({what})")
            it.setData(Qt.ItemDataRole.UserRole, r["id"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            self.rules_list.addItem(it)

    def _rule_idx(self):
        it = self.rules_list.currentItem()
        if not it:
            return None
        rid = it.data(Qt.ItemDataRole.UserRole)
        return next((i for i, r in enumerate(self.cfg["rules"]) if r["id"] == rid), None)

    def _windows(self):
        return self.app.bridge.try_call("list_windows") or []

    def _rule_add(self):
        d = RuleDialog(self.app, None, bool(self.cfg.get("allow_title_regex")), self._windows(), self)
        if d.exec():
            self.cfg.setdefault("rules", []).append(d.rule)
            self._load_rules()
            self._update_dirty()

    def _rule_edit(self):
        i = self._rule_idx()
        if i is None:
            return
        d = RuleDialog(self.app, self.cfg["rules"][i], bool(self.cfg.get("allow_title_regex")), self._windows(), self)
        if d.exec():
            self.cfg["rules"][i] = d.rule
            self._load_rules()
            self._update_dirty()

    def _rule_del(self):
        i = self._rule_idx()
        if i is not None:
            del self.cfg["rules"][i]
            self._load_rules()
            self._update_dirty()

    def _rule_move(self, d: int):
        i = self._rule_idx()
        if i is None or not (0 <= i + d < len(self.cfg["rules"])):
            return
        rs = self.cfg["rules"]
        rs[i], rs[i + d] = rs[i + d], rs[i]
        self._load_rules()
        self.rules_list.setCurrentRow(i + d)
        self._update_dirty()

    def _rule_apply(self):
        if self.cfg != self.saved:
            QMessageBox.information(self, "Rules", "Apply your changes first.")
            return
        res = self.app.bridge.try_call("rules_apply_now") or []
        QMessageBox.information(self, "Rules", f"{len(res)} window(s) placed by rules." if res else "No open window matches a rule.")

    def _monitors(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.mon_lbl = QLabel()
        self.mon_lbl.setWordWrap(True)
        v.addWidget(self.mon_lbl)
        v.addWidget(self.b.check("auto_switch_profiles", "Switch profile automatically when the monitor setup matches"))
        self.prof_list = QListWidget()
        v.addWidget(self.prof_list, 1)
        row = QHBoxLayout()
        b = QPushButton("Create profile for the current monitors…")
        b.clicked.connect(self._prof_add)
        row.addWidget(b)
        b = QPushButton("Remove")
        b.clicked.connect(self._prof_del)
        row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(QLabel("A profile can use its own default grid, spacing and login layout."))
        return w

    def _load_profiles(self):
        outs = self.app.bridge.try_call("list_outputs") or []
        self.mon_lbl.setText("Connected monitors:\n" + "\n".join(f"• {o.get('description') or o['name']}  {o['rect']['w']}×{o['rect']['h']} at {o['rect']['x']},{o['rect']['y']}  scale {o['scale']}{'  (primary)' if o.get('primary') else ''}" for o in outs))
        self.prof_list.clear()
        for p in self.cfg.get("profiles", []):
            g = p.get("default_grid")
            self.prof_list.addItem(f"{'● ' if self.cfg.get('active_profile') == p['name'] else ''}{p['name']}" + (f"  grid {g['cols']}×{g['rows']}" if g else "") + (f"  layout {p['login_layout']}" if p.get("login_layout") else ""))

    def _prof_add(self):
        d0 = self.app.bridge.try_call("diagnose") or {}
        outs = d0.get("outputs") or []
        topo = d0.get("topology_signature")
        if not topo:
            QMessageBox.warning(self, "Monitor profile", "No monitors are known on this desktop.")
            return
        d = QDialog(self)
        d.setWindowTitle("New monitor profile")
        f = QFormLayout(d)
        name = QLineEdit(f"{len(outs)} monitor{'s' if len(outs) != 1 else ''}")
        f.addRow("Name", name)
        cols = QSpinBox()
        cols.setRange(1, 10)
        cols.setValue(getp(self.cfg, "appearance.default_grid.cols", 6))
        rows = QSpinBox()
        rows.setRange(1, 10)
        rows.setValue(getp(self.cfg, "appearance.default_grid.rows", 6))
        f.addRow("Default columns", cols)
        f.addRow("Default rows", rows)
        lay = QComboBox()
        lay.addItem("(none)", None)
        for n in self._layout_names():
            lay.addItem(n, n)
        f.addRow("Layout when this setup appears", lay)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        f.addRow(bb)
        if not d.exec():
            return
        self.cfg.setdefault("profiles", []).append({"name": name.text().strip() or "Profile", "topology": topo, "default_grid": {"cols": cols.value(), "rows": rows.value()},
                                                     "spacing": None, "login_layout": lay.currentData()})
        self._load_profiles()
        self._update_dirty()

    def _prof_del(self):
        i = self.prof_list.currentRow()
        if i >= 0:
            del self.cfg["profiles"][i]
            self._load_profiles()
            self._update_dirty()

    def _diagnostics(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.diag = QPlainTextEdit()
        self.diag.setReadOnly(True)
        f = self.diag.font()
        f.setFamily("monospace")
        self.diag.setFont(f)
        v.addWidget(self.diag, 1)
        row = QHBoxLayout()
        b = QPushButton("Refresh")
        b.clicked.connect(self._refresh_diag)
        row.addWidget(b)
        b = QPushButton("Copy report")
        b.clicked.connect(self._copy_diag)
        row.addWidget(b)
        b = QPushButton("Show recent log (titles hidden)")
        b.clicked.connect(self._show_log)
        row.addWidget(b)
        row.addStretch(1)
        row.addWidget(QLabel("Log detail"))
        row.addWidget(self.b.combo("diagnostics.log_level", [("error", "Errors"), ("warn", "Warnings"), ("info", "Normal"), ("debug", "Debug")]))
        v.addLayout(row)
        v.addWidget(QLabel("Fractile sends nothing anywhere. Reports are only copied when you choose to."))
        return w

    def _refresh_diag(self):
        d = self.app.bridge.try_call("diagnose")
        if not d:
            self.diag.setPlainText("Service not running.")
            return
        caps = d["capabilities"]
        lines = [f"Fractile {d['version']}", f"OS: {d['environment']['os_pretty_name']}", f"Session: {d['environment']['session_type']}  Desktop: {d['environment']['current_desktop']}",
                 f"Adapter: {caps['adapter']} {caps['adapter_version']}  Compositor: {caps['compositor']}", "", "What works on this desktop:"]
        for op, lvl in caps["operations"].items():
            level = lvl.get("level") if isinstance(lvl, dict) else lvl
            note = lvl.get("note", "") if isinstance(lvl, dict) else ""
            lines.append(f"  {op:<22} {'yes' if level == 'full' else level}{(': ' + note) if note else ''}")
        for n in caps.get("notes", []):
            lines.append("  note: " + n)
        lines.append("")
        for o in d.get("outputs") or []:
            lines.append(f"Monitor {o['stable_id']}  {o['name']}  {o['rect']['w']}x{o['rect']['h']}+{o['rect']['x']}+{o['rect']['y']}  work area {o['work_area']['w']}x{o['work_area']['h']}  scale {o['scale']}")
        lines.append(f"Windows: {d.get('window_count')} ({d.get('placeable_windows')} placeable)")
        pl = d.get("placement_latency_ms", {})
        lines.append(f"Placement latency: median {pl.get('p50')} ms, 95th percentile {pl.get('p95')} ms ({pl.get('samples')} samples)")
        lines.append(f"Snapshots: {d['store']['snapshots']}  Layouts: {d['store']['layouts']}")
        lines.append(f"Autostart: {'on' if d['autostart'].get('enabled') else 'off'} ({d['autostart'].get('mode')})")
        if d.get("warnings"):
            lines.append("")
            lines += ["⚠ " + x for x in d["warnings"]]
        self.diag.setPlainText("\n".join(lines))
        self._diag_raw = d

    def _copy_diag(self):
        d = getattr(self, "_diag_raw", None) or self.app.bridge.try_call("diagnose")
        if d:
            QGuiApplication.clipboard().setText(json.dumps(d, indent=2))

    def _show_log(self):
        lines = self.app.bridge.try_call("logs", {"lines": 400, "redacted": True}) or []
        self.diag.setPlainText("\n".join(lines))

    def _about(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        t = QLabel(f"<h2>Fractile {__version__}</h2><p>Grid window placement and session restore for Linux.</p>"
                   "<p>Licensed under the GNU GPL v3 or later.</p><p>No telemetry, no network access. Runs as your user only.</p>"
                   "<p>Fractile restores window placement. It cannot restore unsaved document contents.</p>")
        t.setWordWrap(True)
        t.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(t)
        b = QPushButton("Run the first-run setup again")
        b.clicked.connect(self.app.show_wizard)
        v.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        v.addStretch(1)
        return w
