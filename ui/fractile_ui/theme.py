"""Palette and drawing constants. Follows the system palette unless the user
picks light, dark or high contrast."""
from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QStyleFactory


def apply_theme(app: QApplication, theme: str, font_scale: float = 1.0) -> None:
    if theme in ("light", "dark", "high_contrast"):
        app.setStyle(QStyleFactory.create("Fusion"))
        p = QPalette()
        if theme == "light":
            colors = dict(win="#f6f6f8", base="#ffffff", text="#1d1d22", btn="#ececf0", hl="#3b6fe0", hltext="#ffffff", mid="#c9c9d1")
        elif theme == "dark":
            colors = dict(win="#232328", base="#1b1b1f", text="#ececf1", btn="#2e2e35", hl="#5b8cff", hltext="#0b0b0e", mid="#4a4a55")
        else:
            colors = dict(win="#000000", base="#000000", text="#ffffff", btn="#000000", hl="#ffff00", hltext="#000000", mid="#ffffff")
        p.setColor(QPalette.ColorRole.Window, QColor(colors["win"]))
        p.setColor(QPalette.ColorRole.WindowText, QColor(colors["text"]))
        p.setColor(QPalette.ColorRole.Base, QColor(colors["base"]))
        p.setColor(QPalette.ColorRole.AlternateBase, QColor(colors["btn"]))
        p.setColor(QPalette.ColorRole.Text, QColor(colors["text"]))
        p.setColor(QPalette.ColorRole.Button, QColor(colors["btn"]))
        p.setColor(QPalette.ColorRole.ButtonText, QColor(colors["text"]))
        p.setColor(QPalette.ColorRole.Highlight, QColor(colors["hl"]))
        p.setColor(QPalette.ColorRole.HighlightedText, QColor(colors["hltext"]))
        p.setColor(QPalette.ColorRole.Mid, QColor(colors["mid"]))
        p.setColor(QPalette.ColorRole.ToolTipBase, QColor(colors["base"]))
        p.setColor(QPalette.ColorRole.ToolTipText, QColor(colors["text"]))
        app.setPalette(p)
    f = app.font()
    if font_scale and abs(font_scale - 1.0) > 0.01:
        base = f.pointSizeF() if f.pointSizeF() > 0 else 10.0
        f.setPointSizeF(base * font_scale)
        app.setFont(f)


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
