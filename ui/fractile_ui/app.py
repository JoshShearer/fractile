"""fractile-ui: resident desktop UI. The service (fractiled) owns all window
control; this process only draws panels, dialogs and the tray icon.

Single instance: if a UI is already attached to the service, a second
launch forwards its request (show settings, dashboard, panel) and exits.
"""
from __future__ import annotations

import argparse
import os
import sys

from PyQt6.QtCore import QRect, QTimer
from PyQt6.QtGui import QAction, QGuiApplication, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import APP_ID, __version__
from .bridge import Bridge
from .ipc import Client, RpcError
from .panel import PanelController
from .theme import apply_theme


def app_icon() -> QIcon:
    ic = QIcon.fromTheme(APP_ID)
    if ic.isNull():
        here = os.path.dirname(os.path.abspath(__file__))
        for p in (os.path.join(here, "data", f"{APP_ID}.svg"), os.path.join(here, "..", "..", "packaging", "icons", f"{APP_ID}.svg")):
            if os.path.exists(p):
                return QIcon(p)
    return ic


class FractileApp:
    def __init__(self, qapp: QApplication, args):
        self.qapp = qapp
        self.args = args
        self.bridge = Bridge("ui")
        self.wayland = QGuiApplication.platformName().startswith("wayland")
        self.panel = PanelController(self)
        self._cfg: dict | None = None
        self.settings_win = None
        self.dashboard_win = None
        self.palette_win = None
        self.recovery_win = None
        self.wizard_win = None
        self._wizard_pending = False
        self.tray: QSystemTrayIcon | None = None
        self.status: dict = {}
        self.bridge.event.connect(self.on_event)
        self.bridge.connected.connect(self.on_connected)
        self.bridge.disconnected.connect(self.on_disconnected)

    # ---- service ----
    def start(self):
        self.bridge.keep_connected("ui")

    def config(self) -> dict:
        if self._cfg is None:
            r = self.bridge.try_call("get_config")
            self._cfg = (r or {}).get("config") or {}
        return self._cfg

    def reload_config(self):
        self._cfg = None
        cfg = self.config()
        apply_theme(self.qapp, cfg.get("appearance", {}).get("theme", "system"), cfg.get("appearance", {}).get("font_scale", 1.0))
        self.update_tray()

    def on_connected(self, info: dict):
        self.status = self.bridge.try_call("status", {}, {}) or {}
        self.reload_config()
        self.register_own_windows()
        if not self.config().get("general", {}).get("first_run_complete", False):
            self._wizard_pending = True
            QTimer.singleShot(300, self.maybe_show_wizard)
        if self.status.get("config_error"):
            self.notify("Settings could not be loaded", f"Fractile is using defaults. Your file was not changed. {self.status['config_error'][:200]}")
        a = self.args
        if a.launcher and self.config().get("general", {}).get("first_run_complete", False):
            self.bridge.try_call("show_panel")
        elif a.settings:
            self.show_settings(a.page)
        elif a.dashboard:
            self.show_dashboard()
        elif a.panel:
            self.bridge.try_call("show_panel")

    def on_disconnected(self):
        self.panel.hide(notify=False)
        if self.tray:
            self.tray.setToolTip("Fractile: service not running")

    def on_event(self, ev: str, data):
        data = data or {}
        if ev == "show_panel":
            self.panel.show(data)
        elif ev == "hide_panel":
            self.panel.hide(notify=False)
        elif ev == "show_settings":
            self.show_settings(data.get("page"))
        elif ev == "show_dashboard":
            self.show_dashboard()
        elif ev == "show_palette":
            self.show_palette()
        elif ev == "show_recovery":
            self.show_recovery(data)
        elif ev == "config_changed":
            self.reload_config()
            if self.settings_win and self.settings_win.isVisible():
                self.settings_win.reload_from_service()
        elif ev == "notice":
            self.notify("Fractile", data.get("message", ""))
        elif ev == "windows_changed":
            t = (self.panel.payload or {}).get("target")
            if self.panel.visible and t:
                ws = self.bridge.try_call("list_windows", {"all": True}, None)
                if ws is not None:
                    self.panel.target_closed(any(w["id"] == t["id"] for w in ws))
        elif ev in ("restore_progress", "restore_started", "restore_finished"):
            if self.dashboard_win:
                self.dashboard_win.on_restore_event(ev, data)
            if self.recovery_win:
                self.recovery_win.on_restore_event(ev, data)
            if ev == "restore_finished":
                st = data.get("state")
                rep = data.get("report") or {}
                n = len(rep.get("items") or [])
                ok = sum(1 for i in rep.get("items") or [] if i.get("status") in ("ALREADY_PRESENT", "LAUNCHED_AND_PLACED", "PLACED_ONLY"))
                if st != "completed" or not (self.dashboard_win and self.dashboard_win.isVisible()):
                    self.notify("Restore " + str(st), f"{ok} of {n} windows restored.")
        elif ev in ("snapshots_changed", "layouts_changed"):
            if self.dashboard_win:
                self.dashboard_win.reload()
        elif ev == "daemon_exit":
            self.panel.hide(notify=False)
        elif ev == "outputs_changed":
            if self.panel.visible:
                self.panel.hide()

    # ---- own windows (never placement targets) ----
    def own_window_ids(self) -> list[str]:
        ids = []
        if QGuiApplication.platformName() == "xcb":
            for w in self.qapp.topLevelWidgets():
                if w.isVisible():
                    ids.append(f"x11:0x{int(w.winId()):x}")
        return ids

    def register_own_windows(self):
        self.bridge.try_call("ui_register", {"windows": self.own_window_ids()})

    def focus_own(self, w):
        if QGuiApplication.platformName() == "xcb":
            self.bridge.try_call("focus_own_window", {"window": f"x11:0x{int(w.winId()):x}"})
        elif self.wayland:
            # Async: on Wayland the surface only maps once the event loop
            # runs, and the daemon looks the window up in the compositor.
            self.bridge.call_async("focus_own_window", {"title": w.windowTitle()})

    def position_own(self, w, rect: QRect):
        """Wayland clients cannot position themselves; ask the compositor
        integration to do it. On X11, Qt's move() already did."""
        if self.wayland:
            self.bridge.call_async("place_own_window", {"title": w.windowTitle(), "rect": {"x": rect.x(), "y": rect.y(), "w": rect.width(), "h": rect.height()}})

    # ---- windows ----
    def show_settings(self, page: str | None = None):
        from .settings import SettingsWindow
        if self.settings_win is None:
            self.settings_win = SettingsWindow(self)
        self.settings_win.open_page(page)
        self._present(self.settings_win)

    def show_dashboard(self):
        from .dashboard import DashboardWindow
        if self.dashboard_win is None:
            self.dashboard_win = DashboardWindow(self)
        self.dashboard_win.reload()
        self._present(self.dashboard_win)

    def show_palette(self):
        from .palette import CommandPalette
        if self.palette_win is None:
            self.palette_win = CommandPalette(self)
        self.palette_win.popup()
        self.register_own_windows()
        self.focus_own(self.palette_win)

    def show_recovery(self, data: dict):
        from .recovery import RecoveryDialog
        if self.recovery_win is not None:
            self.recovery_win.close()
        self.recovery_win = RecoveryDialog(self, data)
        self._present(self.recovery_win)

    def maybe_show_wizard(self):
        """First-run setup never covers a panel the user just opened; it
        waits until the panel is closed (PanelController.hide calls this)."""
        if not self._wizard_pending or self.panel.visible:
            return
        self._wizard_pending = False
        self.show_wizard()

    def show_wizard(self):
        from .wizard import FirstRunWizard
        if self.wizard_win is None:
            self.wizard_win = FirstRunWizard(self)
        self._present(self.wizard_win)

    def _present(self, w):
        w.show()
        w.raise_()
        w.activateWindow()
        self.register_own_windows()
        QTimer.singleShot(0, lambda: self.focus_own(w))

    # ---- tray (DV-19) ----
    def update_tray(self):
        want = bool(self.config().get("general", {}).get("tray_icon", True))
        if not want or not QSystemTrayIcon.isSystemTrayAvailable():
            if self.tray:
                self.tray.hide()
            return
        if self.tray is None:
            self.tray = QSystemTrayIcon(app_icon())
            self.tray.setToolTip("Fractile")
            m = QMenu()
            def add(text, fn):
                a = QAction(text, m)
                a.triggered.connect(fn)
                m.addAction(a)
            add("Show panel", lambda: self.bridge.try_call("show_panel"))
            add("Command palette", self.show_palette)
            m.addSeparator()
            add("Save workspace now", self.save_now)
            add("Restore latest…", lambda: self.show_recovery({"snapshot_id": None, "reason": "tray"}))
            add("Workspace dashboard", self.show_dashboard)
            m.addSeparator()
            add("Settings", lambda: self.show_settings())
            add("Quit Fractile", self.quit_all)
            self.tray.setContextMenu(m)
            self.tray.activated.connect(lambda r: r == QSystemTrayIcon.ActivationReason.Trigger and self.bridge.try_call("show_panel"))
            self._tray_menu = m
        self.tray.show()

    def save_now(self):
        def ok(r):
            self.notify("Workspace saved", f"Snapshot {r.get('snapshot_id', '')[:8]} saved.")
        self.bridge.call_async("snapshot_save", {}, ok, lambda c, m: self.notify("Save failed", m))

    def notify(self, title: str, msg: str):
        if self.tray and self.tray.isVisible() and QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(title, msg, app_icon(), 6000)
            return
        try:
            import subprocess
            subprocess.Popen(["notify-send", "--app-name=Fractile", "--icon", APP_ID, title, msg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            print(f"{title}: {msg}", file=sys.stderr)

    def quit_all(self):
        r = QMessageBox.question(None, "Quit Fractile", "Stop the Fractile service too?\n\nShortcuts and automatic snapshots stop until you start Fractile again.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Cancel:
            return
        if r == QMessageBox.StandardButton.Yes:
            self.bridge.try_call("quit")
        self.bridge.stop()
        self.qapp.quit()


def forward_if_running(args) -> bool:
    """If another UI instance is attached, forward the request and exit."""
    try:
        c = Client("ui-launcher", timeout=5)
    except RpcError:
        return False
    try:
        st = c.call("status")
        if not st.get("ui_attached"):
            return False
        if args.launcher:
            # Divvy: launching the app again shows the panel
            c.call("show_panel")
        elif args.settings:
            c.call("show_settings", {"page": args.page})
        elif args.dashboard:
            c.call("show_dashboard")
        elif args.palette:
            c.call("show_palette")
        else:
            c.call("show_panel" if args.panel else "show_settings", {})
        return True
    except RpcError:
        return False
    finally:
        c.close()


def ensure_service() -> None:
    """Start the service if it is not running (e.g. launched from the menu
    before login autostart, or after a crash)."""
    try:
        Client("ui-probe", timeout=3).close()
        return
    except RpcError:
        pass
    import shutil
    import subprocess
    if shutil.which("systemctl") and subprocess.run(["systemctl", "--user", "start", "fractile.service"], capture_output=True).returncode == 0:
        return
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    exe = os.path.join(here, "fractiled")
    if not os.path.exists(exe):
        exe = shutil.which("fractiled") or "fractiled"
    try:
        subprocess.Popen([exe], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fractile-ui", description="Fractile desktop UI")
    ap.add_argument("--settings", action="store_true", help="open settings")
    ap.add_argument("--page", default=None, help="settings page to open")
    ap.add_argument("--dashboard", action="store_true", help="open the workspace dashboard")
    ap.add_argument("--panel", action="store_true", help="show the placement panel")
    ap.add_argument("--launcher", action="store_true", help="started from the app menu: the panel (or first-run setup)")
    ap.add_argument("--palette", action="store_true", help="open the command palette")
    ap.add_argument("--version", action="version", version=f"fractile-ui {__version__}")
    args = ap.parse_args((argv or sys.argv)[1:])
    if os.geteuid() == 0:
        print("fractile-ui: refusing to run as root", file=sys.stderr)
        return 2
    if forward_if_running(args):
        return 0
    ensure_service()
    QGuiApplication.setDesktopFileName(APP_ID)
    qapp = QApplication(sys.argv[:1])
    qapp.setApplicationName("Fractile")
    qapp.setApplicationVersion(__version__)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(app_icon())
    app = FractileApp(qapp, args)
    app.start()
    return qapp.exec()
