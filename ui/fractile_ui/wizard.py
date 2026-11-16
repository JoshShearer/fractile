"""First-run setup: honest capability check, a free panel shortcut, restore
policy and autostart. Nothing is changed until Finish."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QLabel, QRadioButton, QVBoxLayout, QWizard, QWizardPage

from .widgets import ShortcutEdit

CANDIDATES = ["Super+Alt+G", "Ctrl+Alt+G", "Super+Alt+Space", "Ctrl+Alt+Shift+G", "Super+Ctrl+G"]


class FirstRunWizard(QWizard):
    def __init__(self, app):
        # A top-level window, not a dialog: a parentless dialog is transient for
        # the whole client group on X11 and the WM would stack it above the
        # placement panel.
        super().__init__(None, Qt.WindowType.Window)
        self.app = app
        self.setWindowTitle("Welcome to Fractile")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.resize(640, 480)
        self.addPage(self._welcome())
        self.addPage(self._desktop())
        self.addPage(self._shortcut())
        self.addPage(self._session())
        self.addPage(self._finish())
        self.accepted.connect(self.save)

    def _page(self, title, sub=""):
        p = QWizardPage()
        p.setTitle(title)
        if sub:
            p.setSubTitle(sub)
        v = QVBoxLayout(p)
        return p, v

    def _welcome(self):
        p, v = self._page("Place windows on a grid", "Open the panel, drag across the grid, and the window fills that area.")
        t = QLabel("Fractile also saves which apps and windows you had open and puts them back after a restart.\n\n"
                   "It restores windows (monitor, workspace, size, position). It cannot bring back unsaved work inside an app; "
                   "apps with their own session restore, like browsers, reopen their own tabs.")
        t.setWordWrap(True)
        v.addWidget(t)
        st = self.app.status or {}
        if st.get("window_control") is False:
            if st.get("restore_without_geometry"):
                msg = ("On this desktop Fractile cannot move or resize windows, so the panel and snapping will not work "
                       "here. Session restore still reopens your apps and puts windows back on their workspace, "
                       "maximized or full screen, but not at their position or size. The next page says why.")
            else:
                msg = ("On this desktop Fractile cannot move or resize windows, so neither the panel nor session restore "
                       "will work here. The next page says why.")
            w = QLabel(msg)
            w.setWordWrap(True)
            # A bold font, not a style sheet: a style sheet here drops the
            # theme's text colour (black on the dark theme).
            f = w.font()
            f.setBold(True)
            w.setFont(f)
            v.addWidget(w)
        return p

    def _desktop(self):
        p, v = self._page("Your desktop", "What Fractile can do here")
        d = self.app.bridge.try_call("diagnose") or {}
        caps = d.get("capabilities") or {}
        ops = caps.get("operations") or {}
        def line(op, text):
            lvl = ops.get(op)
            level = lvl.get("level") if isinstance(lvl, dict) else lvl
            if level == "full":
                return "\u2713 " + text
            if level == "partial":
                return "\u2713 " + text + " (works, with limits; see Settings \u2192 Diagnostics)"
            return "\u2717 " + text + " (not available on this desktop)"
        lines = [f"Desktop: {d.get('environment', {}).get('current_desktop', '?')} ({d.get('environment', {}).get('session_type', '?')})", ""]
        lines.append(line("move_resize", "Move and resize windows"))
        lines.append(line("register_hotkey", "Global keyboard shortcuts"))
        lines.append(line("list_windows", "See open windows (for session restore)"))
        lines.append(line("move_to_workspace", "Move windows between workspaces"))
        for n in caps.get("notes") or []:
            lines.append("")
            lines.append(n)
        for w in d.get("warnings") or []:
            lines.append("")
            lines.append("⚠ " + w)
        t = QLabel("\n".join(lines))
        t.setWordWrap(True)
        v.addWidget(t)
        v.addStretch(1)
        return p

    def _shortcut(self):
        p, v = self._page("Panel shortcut", "Press this anywhere to open the panel for the focused window.")
        free = None
        current = (self.app.config() or {}).get("general", {}).get("panel_shortcut")
        hk_errors = (self.app.status or {}).get("hotkey_errors") or {}
        if current and "panel" not in hk_errors and "*" not in hk_errors:
            # Keep a shortcut the user already set and that is working.
            free = current
        for c in ([] if free else CANDIDATES):
            # As the panel shortcut itself: its own current binding is not "in use".
            r = self.app.bridge.try_call("shortcut_check", {"accelerator": c, "own_id": "panel"})
            if r and r.get("free"):
                free = c
                break
        self.sc = ShortcutEdit(self.app, "global", free, "panel")
        self.sc.set_accessible_label("Panel shortcut")
        v.addWidget(self.sc)
        t = QLabel((f"Current shortcut: {free}." if free == current else f"Suggested: {free}, which is free on this desktop.") if free else "None of the usual combinations is free. Click the button and press one you like, or leave it empty and use the tray icon or app menu.")
        t.setWordWrap(True)
        v.addWidget(t)
        v.addStretch(1)
        return p

    def _session(self):
        p, v = self._page("After a restart", "What should happen to your windows when you log in?")
        self.r_manual = QRadioButton("Nothing automatic. I'll save and restore when I want (recommended to start)")
        self.r_ask = QRadioButton("Ask me each time I log in")
        self.r_auto = QRadioButton("Restore my last session automatically")
        self.r_manual.setChecked(True)
        for r in (self.r_manual, self.r_ask, self.r_auto):
            v.addWidget(r)
        t = QLabel("Apps that were closed are only reopened if you allow it. Fractile never runs commands from imported files.")
        t.setWordWrap(True)
        v.addWidget(t)
        v.addStretch(1)
        return p

    def _finish(self):
        p, v = self._page("Ready")
        self.auto = QCheckBox("Start Fractile when I log in")
        self.auto.setChecked(True)
        self.tray = QCheckBox("Show a tray icon (if your desktop has a tray)")
        self.tray.setChecked(True)
        v.addWidget(self.auto)
        v.addWidget(self.tray)
        v.addStretch(1)
        return p

    def save(self):
        policy = "ask" if self.r_ask.isChecked() else "last_clean" if self.r_auto.isChecked() else "manual"
        patch = {"general": {"first_run_complete": True, "autostart": self.auto.isChecked(), "tray_icon": self.tray.isChecked(), "panel_shortcut": self.sc.value},
                 "session": {"policy": policy}}
        try:
            r = self.app.bridge.call("set_config", {"patch": patch})
            errs = (r or {}).get("hotkey_errors") or {}
            if errs:
                self.app.notify("Shortcut not active", "; ".join(errs.values()))
        except Exception as e:  # noqa: BLE001
            self.app.notify("Setup not saved", getattr(e, "message", str(e)))
        self.app.reload_config()
