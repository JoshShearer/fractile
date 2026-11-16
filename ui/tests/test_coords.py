import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from PyQt6.QtCore import QRect
    from fractile_ui.panel import to_logical
except ImportError:  # PyQt6 missing: nothing to test here
    QRect = None


class FakeScreen:
    def __init__(self, x, y, w, h, dpr):
        self.g, self.dpr = QRect(x, y, w, h), dpr

    def geometry(self):
        return self.g

    def devicePixelRatio(self):
        return self.dpr


@unittest.skipIf(QRect is None, "PyQt6 not installed")
class CoordsTest(unittest.TestCase):
    def test_wayland_rect_on_a_scaled_monitor_is_used_as_is(self):
        # KWin with a 1600x1200 monitor at scale 1 and a 5120x2160 one at
        # scale 2.7 beside it (1896x800 logical at x=1600), as in the VM.
        b = FakeScreen(1600, 0, 1896, 800, 2.7)
        r = to_logical({"x": 1600, "y": 0, "w": 1896, "h": 800}, b, logical=True)
        self.assertEqual((r.x(), r.y(), r.width(), r.height()), (1600, 0, 1896, 800))

    def test_x11_device_pixels_are_divided_by_the_scale(self):
        b = FakeScreen(1920, 0, 1920, 1080, 2.0)  # device rect 3840,0 3840x2160
        r = to_logical({"x": 3840, "y": 0, "w": 3840, "h": 2160}, b, logical=False)
        self.assertEqual((r.x(), r.y(), r.width(), r.height()), (1920, 0, 1920, 1080))


if __name__ == "__main__":
    unittest.main()
