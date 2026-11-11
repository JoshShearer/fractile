"""Pure grid-selection logic for the panel (no Qt), unit tested.

Coordinates are inclusive cell indices like the service's `Selection`.
Subdivision (Alt) doubles both axes; converting back keeps the covered
area (DV-05: reversible without losing intent).
"""
from __future__ import annotations

from dataclasses import dataclass

MIN_CELLS = 1
MAX_BASE = 10
MAX_SUBDIVIDED = 20
FACTOR = 2


@dataclass(frozen=True)
class Sel:
    col0: int
    row0: int
    col1: int
    row1: int

    @staticmethod
    def corners(a: tuple[int, int], b: tuple[int, int]) -> "Sel":
        return Sel(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))

    def as_dict(self) -> dict:
        return {"col0": self.col0, "row0": self.row0, "col1": self.col1, "row1": self.row1}

    def cells(self) -> int:
        return (self.col1 - self.col0 + 1) * (self.row1 - self.row0 + 1)


@dataclass
class GridModel:
    cols: int = 6
    rows: int = 6
    subdivided: bool = False
    anchor: tuple[int, int] | None = None
    cursor: tuple[int, int] | None = None
    selection: Sel | None = None

    # ---- size ----
    @property
    def eff_cols(self) -> int:
        return self.cols * FACTOR if self.subdivided else self.cols

    @property
    def eff_rows(self) -> int:
        return self.rows * FACTOR if self.subdivided else self.rows

    def eff_grid(self) -> dict:
        return {"cols": self.eff_cols, "rows": self.eff_rows}

    def resize(self, dcols: int = 0, drows: int = 0) -> bool:
        """Add/remove base rows/columns (DV-07). Clears the selection when it
        would no longer fit."""
        nc = max(MIN_CELLS, min(MAX_BASE, self.cols + dcols))
        nr = max(MIN_CELLS, min(MAX_BASE, self.rows + drows))
        if (nc, nr) == (self.cols, self.rows):
            return False
        self.cols, self.rows = nc, nr
        self._clamp()
        return True

    def _clamp(self) -> None:
        c, r = self.eff_cols - 1, self.eff_rows - 1
        if self.selection and (self.selection.col1 > c or self.selection.row1 > r):
            self.selection = None
            self.anchor = None
        if self.cursor:
            self.cursor = (min(self.cursor[0], c), min(self.cursor[1], r))
        if self.anchor:
            self.anchor = (min(self.anchor[0], c), min(self.anchor[1], r))

    # ---- subdivision ----
    def set_subdivided(self, on: bool) -> None:
        if on == self.subdivided:
            return
        if on and (self.cols * FACTOR > MAX_SUBDIVIDED or self.rows * FACTOR > MAX_SUBDIVIDED):
            return
        f = FACTOR
        if on:
            m = lambda p: (p[0] * f, p[1] * f)  # noqa: E731
            if self.selection:
                s = self.selection
                self.selection = Sel(s.col0 * f, s.row0 * f, s.col1 * f + f - 1, s.row1 * f + f - 1)
        else:
            m = lambda p: (p[0] // f, p[1] // f)  # noqa: E731
            if self.selection:
                s = self.selection
                self.selection = Sel(s.col0 // f, s.row0 // f, s.col1 // f, s.row1 // f)
        if self.anchor:
            self.anchor = m(self.anchor)
        if self.cursor:
            self.cursor = m(self.cursor)
        self.subdivided = on

    # ---- mouse ----
    def press(self, cell: tuple[int, int]) -> Sel:
        self.anchor = cell
        self.cursor = cell
        self.selection = Sel.corners(cell, cell)
        return self.selection

    def drag(self, cell: tuple[int, int]) -> Sel | None:
        if self.anchor is None:
            return None
        self.cursor = cell
        self.selection = Sel.corners(self.anchor, cell)
        return self.selection

    def hover(self, cell: tuple[int, int]) -> Sel:
        """Without a button pressed, hovering previews a single cell."""
        return Sel.corners(cell, cell)

    # ---- keyboard (arrows move, Shift+arrows extend) ----
    def key_move(self, dx: int, dy: int, extend: bool) -> Sel:
        if self.cursor is None:
            self.cursor = (0, 0)
            self.anchor = (0, 0)
        else:
            c = (max(0, min(self.eff_cols - 1, self.cursor[0] + dx)), max(0, min(self.eff_rows - 1, self.cursor[1] + dy)))
            self.cursor = c
            if not extend or self.anchor is None:
                self.anchor = c
        self.selection = Sel.corners(self.anchor, self.cursor)
        return self.selection

    def reset(self) -> None:
        self.anchor = self.cursor = self.selection = None
        self.subdivided = False
