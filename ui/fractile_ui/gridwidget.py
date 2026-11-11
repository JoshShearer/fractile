"""Divvy-style selection grid: click-and-drag a rectangle of cells."""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
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
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cols, rows = self.m.eff_cols, self.m.eff_rows
        cw = (self.width() - 1) / cols
        ch = (self.height() - 1) / rows
        dark = is_dark(self)
        base = QColor(255, 255, 255, 18) if dark else QColor(0, 0, 0, 14)
        line = QColor(255, 255, 255, 50) if dark else QColor(0, 0, 0, 45)
        hl = accent(self)
        sel = self.shown_selection()
        gap = 2.0 if cw > 10 else 1.0
        for r in range(rows):
            for c in range(cols):
                rect = QRectF(c * cw + gap / 2, r * ch + gap / 2, cw - gap, ch - gap)
                inside = sel is not None and sel.col0 <= c <= sel.col1 and sel.row0 <= r <= sel.row1
                if inside:
                    col = QColor(hl)
                    col.setAlpha(230 if self.m.selection is not None else 120)
                    p.setBrush(col)
                else:
                    p.setBrush(base)
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(rect, 3, 3)
        if self.m.subdivided:
            pen = QPen(line)
            pen.setWidthF(1.5)
            p.setPen(pen)
            for c in range(2, cols, 2):
                p.drawLine(int(c * cw), 0, int(c * cw), self.height())
            for r in range(2, rows, 2):
                p.drawLine(0, int(r * ch), self.width(), int(r * ch))
        if self.hasFocus() and self.m.cursor is not None:
            c, r = self.m.cursor
            pen = QPen(QColor(255, 255, 255) if dark else QColor(0, 0, 0))
            pen.setWidthF(2)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(c * cw + 1, r * ch + 1, cw - 2, ch - 2), 3, 3)
        if not self.enabled_for_target:
            p.fillRect(self.rect(), QColor(0, 0, 0, 90))
