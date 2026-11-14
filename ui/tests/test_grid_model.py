import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fractile_ui.grid_model import GridModel, Sel, spoken  # noqa: E402


class GridModelTest(unittest.TestCase):
    def test_drag_any_direction_gives_normalized_selection(self):
        g = GridModel(6, 6)
        g.press((4, 4))
        self.assertEqual(g.drag((1, 2)), Sel(1, 2, 4, 4))

    def test_subdivision_round_trip_keeps_intent(self):
        g = GridModel(6, 6)
        g.press((1, 1))
        g.drag((2, 3))
        g.set_subdivided(True)
        self.assertEqual(g.eff_grid(), {"cols": 12, "rows": 12})
        self.assertEqual(g.selection, Sel(2, 2, 5, 7))
        g.set_subdivided(False)
        self.assertEqual(g.selection, Sel(1, 1, 2, 3))

    def test_subdivision_respects_20_limit(self):
        g = GridModel(10, 10)
        g.set_subdivided(True)
        self.assertEqual(g.eff_grid(), {"cols": 20, "rows": 20})
        g = GridModel(10, 10)
        g.cols = 10
        g.resize(1, 0)
        self.assertEqual(g.cols, 10)

    def test_resize_bounds_and_clears_unfit_selection(self):
        g = GridModel(2, 2)
        g.press((1, 1))
        g.resize(-1, 0)
        self.assertIsNone(g.selection)
        self.assertEqual(g.cols, 1)
        self.assertFalse(g.resize(-1, 0))

    def test_keyboard_extend(self):
        g = GridModel(6, 6)
        g.key_move(0, 0, False)
        g.key_move(1, 0, False)
        s = g.key_move(1, 1, True)
        self.assertEqual(s, Sel(1, 0, 2, 1))
        s = g.key_move(10, 10, True)
        self.assertEqual(s, Sel(1, 0, 5, 5))

    def test_spoken_selection_is_one_based(self):
        self.assertEqual(spoken(Sel(0, 0, 2, 5), 6, 6), "Placement grid: columns 1 to 3, rows 1 to 6, of 6 by 6")
        self.assertEqual(spoken(Sel(4, 2, 4, 2), 12, 12), "Placement grid: column 5, row 3, of 12 by 12")
        self.assertTrue(spoken(None, 6, 6).startswith("Placement grid, 6 by 6. Arrow keys"))


if __name__ == "__main__":
    unittest.main()
