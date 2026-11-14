"""Palette and drawing constants. Follows the system palette unless the user
picks light, dark or high contrast."""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QPalette

try:
    from PyQt6.QtDBus import QDBusConnection, QDBusMessage
except ImportError:  # QtDBus is part of PyQt6, but stay usable without it
    QDBusConnection = QDBusMessage = None
from PyQt6.QtWidgets import QApplication, QStyleFactory


_PALETTES = {
    "light": dict(win="#f6f6f8", base="#ffffff", text="#1d1d22", btn="#ececf0", hl="#3b6fe0", hltext="#ffffff", mid="#c9c9d1"),
    "dark": dict(win="#232328", base="#1b1b1f", text="#ececf1", btn="#2e2e35", hl="#5b8cff", hltext="#0b0b0e", mid="#4a4a55"),
    "high_contrast": dict(win="#000000", base="#000000", text="#ffffff", btn="#000000", hl="#ffff00", hltext="#000000", mid="#ffffff"),
}
# What the platform gave us before we touched anything, so "Follow system"
# can go back to it and the text size is scaled from the original.
_original: dict = {}


def _fixed_palette(name: str) -> QPalette:
    colors = _PALETTES[name]
    p = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "win"), (QPalette.ColorRole.WindowText, "text"), (QPalette.ColorRole.Base, "base"),
                      (QPalette.ColorRole.AlternateBase, "btn"), (QPalette.ColorRole.Text, "text"), (QPalette.ColorRole.Button, "btn"),
                      (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "hl"), (QPalette.ColorRole.HighlightedText, "hltext"),
                      (QPalette.ColorRole.Mid, "mid"), (QPalette.ColorRole.ToolTipBase, "base"), (QPalette.ColorRole.ToolTipText, "text")):
        p.setColor(role, QColor(colors[key]))
    return p


def desktop_color_scheme() -> str | None:
    """The desktop's light/dark preference from the XDG desktop portal
    (org.freedesktop.appearance color-scheme: 1 dark, 2 light, 0 none).
    GNOME, KDE and the gtk portal backend answer it. Qt before 6.5 (Pop!_OS
    22.04 ships 6.4) does not read it, and a qt5ct/qt6ct setup ignores it."""
    if QDBusConnection is None:
        return None
    msg = QDBusMessage.createMethodCall("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                                        "org.freedesktop.portal.Settings", "Read")
    msg.setArguments(["org.freedesktop.appearance", "color-scheme"])
    reply = QDBusConnection.sessionBus().call(msg, timeout=500)
    if reply.type() != QDBusMessage.MessageType.ReplyMessage or not reply.arguments():
        return None
    v = reply.arguments()[0]
    # Read returns the value wrapped in a variant (sometimes twice).
    for _ in range(3):
        if hasattr(v, "variant"):
            v = v.variant()
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return {1: "dark", 2: "light"}.get(v)


def apply_theme(app: QApplication, theme: str, font_scale: float = 1.0, scheme: str | None = None) -> None:
    """scheme: the desktop preference when the caller already has it (from
    a portal change signal); otherwise it is read from the portal."""
    if not _original:
        _original.update(style=app.style().name(), palette=QPalette(app.palette()), font=QFont(app.font()))
    if theme in _PALETTES:
        app.setStyle(QStyleFactory.create("Fusion"))
        app.setPalette(_fixed_palette(theme))
    else:
        # Follow system: start from what the platform gave us, then correct
        # it when the desktop says dark but Qt handed us a light palette (or
        # the other way round).
        if app.style().name().lower() != str(_original["style"]).lower():
            st = QStyleFactory.create(_original["style"])
            if st is not None:
                app.setStyle(st)
        app.setPalette(_original["palette"])
        want = scheme or desktop_color_scheme()
        if want is not None and (want == "dark") != (_original["palette"].color(QPalette.ColorRole.Window).lightness() < 128):
            app.setStyle(QStyleFactory.create("Fusion"))
            app.setPalette(_fixed_palette(want))
    f = QFont(_original["font"])
    if font_scale and abs(font_scale - 1.0) > 0.01:
        base = f.pointSizeF() if f.pointSizeF() > 0 else 10.0
        f.setPointSizeF(base * font_scale)
    app.setFont(f)


def _slot(fn):
    return pyqtSlot(QDBusMessage)(fn) if QDBusMessage is not None else fn


class SchemeWatcher(QObject):
    """Calls back when the desktop switches between light and dark."""

    def __init__(self, on_change):
        super().__init__()
        self.on_change = on_change
        self.connected = False
        if QDBusConnection is None:
            return
        self.connected = QDBusConnection.sessionBus().connect("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                                             "org.freedesktop.portal.Settings", "SettingChanged", self._changed)

    @_slot
    def _changed(self, msg) -> None:
        args = msg.arguments()
        if len(args) >= 3 and args[0] == "org.freedesktop.appearance" and args[1] == "color-scheme":
            v = args[2]
            for _ in range(3):
                if hasattr(v, "variant"):
                    v = v.variant()
            try:
                self.on_change({1: "dark", 2: "light"}.get(int(v)))
            except (TypeError, ValueError):
                self.on_change(None)


def accent(widget) -> QColor:
    return widget.palette().color(QPalette.ColorRole.Highlight)


def is_dark(widget) -> bool:
    return widget.palette().color(QPalette.ColorRole.Window).lightness() < 128


def mute(label, small: bool = False) -> None:
    """Secondary text: blended toward the background but kept readable
    (about 5:1 against the window colour on the stock light and dark
    palettes), unlike QPalette.Mid which is a border colour."""
    p = label.palette()
    fg = p.color(QPalette.ColorRole.WindowText)
    bg = p.color(QPalette.ColorRole.Window)
    a = 0.68
    c = QColor(int(fg.red() * a + bg.red() * (1 - a)), int(fg.green() * a + bg.green() * (1 - a)), int(fg.blue() * a + bg.blue() * (1 - a)))
    p.setColor(QPalette.ColorRole.WindowText, c)
    label.setPalette(p)
    if small:
        f = label.font()
        f.setPointSizeF(f.pointSizeF() * 0.9 if f.pointSizeF() > 0 else 9.0)
        label.setFont(f)
