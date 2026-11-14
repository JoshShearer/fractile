"""Non-interactive preview of the target rectangle (DV-21). It never takes
input or focus, and it is only a drawing: nothing moves until apply (DV-28)."""
from __future__ import annotations

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from .theme import accent


class PreviewOverlay(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.X11BypassWindowManagerHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setWindowTitle("Fractile preview")
        self.label = ""

    def show_target(self, r: dict, screen, label: str = "") -> None:
        """Show over a daemon (device-pixel) rectangle on `screen`."""
        from .panel import to_logical
        self.show_rect(to_logical(r, screen), label)

    def show_rect(self, r: QRect, label: str = "") -> None:
        self.label = label
        self.setGeometry(r)
        if not self.isVisible():
            self.show()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = accent(self)
        fill = QColor(c)
        fill.setAlpha(60)
        p.setBrush(fill)
        pen = QPen(c)
        pen.setWidth(3)
        p.setPen(pen)
        p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 10, 10)
        if self.label:
            p.setPen(QColor(255, 255, 255))
            f = p.font()
            f.setPointSizeF(max(9.0, f.pointSizeF() * 1.2))
            f.setBold(True)
            p.setFont(f)
            br = p.fontMetrics().boundingRect(self.label).adjusted(-10, -6, 10, 6)
            br.moveCenter(self.rect().center())
            bg = QColor(0, 0, 0, 150)
            p.setBrush(bg)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(br, 6, 6)
            p.setPen(QColor(255, 255, 255))
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, self.label)


class CompositorOverlay:
    """Wayland: the preview is drawn by the compositor integration where it
    can draw (GNOME Shell): a client window there would take keyboard focus
    from the panel and cannot position itself. Same interface as
    PreviewOverlay.

    KWin scripts cannot draw. There the UI draws the preview as its own
    input-transparent window, the KWin script puts it on the target rect
    (Wayland clients cannot position themselves), and focus goes back to the
    panel (`refocus`)."""

    def __init__(self, bridge, refocus=None):
        self.bridge = bridge
        self.refocus = refocus
        self._visible = False
        self._last = None
        self._unsupported = False
        self._client: PreviewOverlay | None = None
        self._placed_for = None

    def _use_client_window(self) -> bool:
        caps = self.bridge.try_call("capabilities") or {}
        return caps.get("adapter") == "kwin"

    def show_target(self, r: dict, screen, label: str = "") -> None:
        key = (r["x"], r["y"], r["w"], r["h"], label)
        if self._visible and key == self._last:
            return
        rect = {k: r[k] for k in ("x", "y", "w", "h")}
        if self._client is None:
            if self._unsupported:
                return
            if self.bridge.try_call("overlay_show", {"rect": rect, "label": label}) is not None:
                self._visible, self._last = True, key
                return
            self._unsupported = True
            if not self._use_client_window():
                # The panel's own grid still shows the selection.
                return
            self._client = PreviewOverlay()
        self._show_client(r, rect, screen, label, key)

    def _show_client(self, r: dict, rect: dict, screen, label: str, key) -> None:
        from .panel import to_logical
        c = self._client
        first = not c.isVisible()
        c.label = label
        c.resize(to_logical(r, screen).size())
        if first:
            c.show()
        c.update()
        self._visible, self._last = True, key
        self._place(rect, key, first, tries=10)

    def _place(self, rect: dict, key, first: bool, tries: int) -> None:
        from PyQt6.QtCore import QTimer

        def ok(_res):
            if first and self.refocus:
                self.refocus()

        def fail(_err):
            # Just mapped: the compositor may not list the window yet.
            if tries > 1 and self._visible and self._last == key:
                QTimer.singleShot(60, lambda: self._place(rect, key, first, tries - 1))

        self.bridge.call_async("place_own_window", {"title": self._client.windowTitle(), "rect": rect}, ok=ok, fail=fail)

    def hide(self) -> None:
        if self._client is not None:
            self._client.hide()
        elif self._visible:
            self.bridge.try_call("overlay_hide", {})
        self._visible = False
        self._last = None

    def isVisible(self) -> bool:
        return self._visible
