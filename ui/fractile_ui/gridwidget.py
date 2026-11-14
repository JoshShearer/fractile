"""Divvy-style selection grid: click-and-drag a rectangle of cells."""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from .grid_model import GridModel, Sel
from .theme import accent, is_dark


class GridWidget(QWidget):
    preview = pyqtSignal(object)           # Sel | None
    applied = pyqtSignal(object, bool)     # Sel, shift_held

    def __init__(self, model: GridModel, cell_px: int = 40, parent=None):
        super().__init__(parent)
        self.m = model
        self.cell_px = cell_px
        self.hover: Sel | None = None
        self.dragging = False
        self.enabled_for_target = True
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Placement grid")
        self.setAccessibleDescription("Drag across cells, or use arrow keys and Shift+arrow keys, then Enter to place the window.")

    def sizeHint(self) -> QSize:
        return QSize(self.m.cols * self.cell_px + 1, self.m.rows * self.cell_px + 1)

    def refresh_size(self):
        self.setFixedSize(self.sizeHint())
        self.update()

    def cell_at(self, pos: QPoint) -> tuple[int, int]:
        cw = self.width() / self.m.eff_cols
        ch = self.height() / self.m.eff_rows
        c = int(max(0, min(self.m.eff_cols - 1, pos.x() // cw)))
        r = int(max(0, min(self.m.eff_rows - 1, pos.y() // ch)))
        return c, r

    def shown_selection(self) -> Sel | None:
        if self.m.selection is not None:
            return self.m.selection
        return self.hover

    # ---- mouse ----
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or not self.enabled_for_target:
            return
        self.dragging = True
        self.m.press(self.cell_at(e.position().toPoint()))
        self.preview.emit(self.m.selection)
        self.update()

    def mouseMoveEvent(self, e):
        if not self.enabled_for_target:
            return
        cell = self.cell_at(e.position().toPoint())
        if self.dragging:
            self.m.drag(cell)
            self.preview.emit(self.m.selection)
        elif self.m.selection is None:
            self.hover = self.m.hover(cell)
            self.preview.emit(self.hover)
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or not self.dragging:
            return
        self.dragging = False
        sel = self.m.selection
        if sel is not None:
            self.applied.emit(sel, bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier))

    def leaveEvent(self, _):
        if not self.dragging and self.m.selection is None:
            self.hover = None
            self.preview.emit(None)
            self.update()

    # ---- paint ----
    def _high_contrast(self) -> bool:
        w = self.window()
        return bool(getattr(w, "high_contrast", lambda: False)())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cols, rows = self.m.eff_cols, self.m.eff_rows
        cw = (self.width() - 1) / cols
        ch = (self.height() - 1) / rows
        dark = is_dark(self)
        hc = self._high_contrast()
        hl = accent(self)
        sel = self.shown_selection()
        committed = self.m.selection is not None
        gap = 3.0 if cw > 14 else 1.0
        rad = 5.0 if cw > 14 else 2.0
        # Recessed well behind the cells.
        well = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if not hc:
            p.setPen(QPen(QColor(0, 0, 0, 70 if dark else 22), 1))
            p.setBrush(QColor(0, 0, 0, 80 if dark else 18))
            p.drawRoundedRect(well, rad + 2, rad + 2)
        # Frosted cells: a faint fill with a lit top edge.
        if hc:
            base_top = base_bot = QColor(255, 255, 255, 40)
        elif dark:
            base_top, base_bot = QColor(255, 255, 255, 30), QColor(255, 255, 255, 14)
        else:
            base_top, base_bot = QColor(255, 255, 255, 190), QColor(255, 255, 255, 120)
        edge = QColor(255, 255, 255, 34 if dark else 230)
        for r in range(rows):
            for c in range(cols):
                rect = QRectF(c * cw + gap / 2 + 0.5, r * ch + gap / 2 + 0.5, cw - gap, ch - gap)
                if sel is not None and sel.col0 <= c <= sel.col1 and sel.row0 <= r <= sel.row1:
                    continue
                g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                g.setColorAt(0, base_top)
                g.setColorAt(1, base_bot)
                p.setBrush(g)
                p.setPen(Qt.PenStyle.NoPen if hc else QPen(QColor(0, 0, 0, 0 if dark else 18), 1))
                p.drawRoundedRect(rect, rad, rad)
                if not hc and rect.height() > 8:
                    p.setPen(QPen(edge, 1))
                    p.drawLine(QPointF(rect.left() + rad, rect.top() + 0.5), QPointF(rect.right() - rad, rect.top() + 0.5))
        # The selection is drawn as one pane spanning its cells.
        if sel is not None:
            span = QRectF(sel.col0 * cw + gap / 2 + 0.5, sel.row0 * ch + gap / 2 + 0.5,
                          (sel.col1 - sel.col0 + 1) * cw - gap, (sel.row1 - sel.row0 + 1) * ch - gap)
            if hc:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(hl)
                p.drawRoundedRect(span, rad, rad)
            else:
                strength = 1.0 if committed else 0.55
                glow = QColor(hl)
                for grow, alpha in ((4.0, 26), (2.0, 46)):
                    glow.setAlpha(int(alpha * strength))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(glow)
                    p.drawRoundedRect(span.adjusted(-grow, -grow, grow, grow), rad + grow, rad + grow)
                top, bot = QColor(hl).lighter(128), QColor(hl)
                top.setAlpha(int(235 * strength))
                bot.setAlpha(int(215 * strength))
                g = QLinearGradient(span.topLeft(), span.bottomLeft())
                g.setColorAt(0, top)
                g.setColorAt(1, bot)
                p.setBrush(g)
                p.setPen(QPen(QColor(255, 255, 255, int(110 * strength)), 1))
                p.drawRoundedRect(span, rad, rad)
                # Interior cell seams, so the size in cells stays readable.
                seam = QColor(255, 255, 255, int(40 * strength))
                p.setPen(QPen(seam, 1))
                for c in range(sel.col0 + 1, sel.col1 + 1):
                    x = c * cw + 0.5
                    p.drawLine(QPointF(x, span.top() + 3), QPointF(x, span.bottom() - 3))
                for r in range(sel.row0 + 1, sel.row1 + 1):
                    y = r * ch + 0.5
                    p.drawLine(QPointF(span.left() + 3, y), QPointF(span.right() - 3, y))
        if self.m.subdivided:
            line = QColor(255, 255, 255, 70) if dark or hc else QColor(0, 0, 0, 55)
            pen = QPen(line)
            pen.setWidthF(1.5)
            p.setPen(pen)
            for c in range(2, cols, 2):
                p.drawLine(QPointF(c * cw + 0.5, 2), QPointF(c * cw + 0.5, self.height() - 2))
            for r in range(2, rows, 2):
                p.drawLine(QPointF(2, r * ch + 0.5), QPointF(self.width() - 2, r * ch + 0.5))
        if self.hasFocus() and self.m.cursor is not None:
            c, r = self.m.cursor
            pen = QPen(QColor(255, 255, 255) if dark or hc else QColor(0, 0, 0))
            pen.setWidthF(2)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(c * cw + 1.5, r * ch + 1.5, cw - 2, ch - 2), rad, rad)
        if not self.enabled_for_target:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 110) if dark or hc else QColor(255, 255, 255, 150))
            p.drawRoundedRect(well, rad + 2, rad + 2)
